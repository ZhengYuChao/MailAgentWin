import os
import sys
import time
import json
import subprocess
import urllib.request
import ssl
from urllib.error import URLError, HTTPError
from loguru import logger
from src.config import config


class TunnelManager:
    """管理 ngrok 或 cloudflared 隧道"""
    
    def __init__(self, port: int = 54321):
        self.port = port
        self.ngrok_process = None
        self.cloudflared_process = None
        self.allowed_host_keyword = ""
        self._restart_attempts = 0

    def get_allowed_hosts(self) -> list[str]:
        """返回所有允许的 Host 列表（包括 localhost, 127.0.0.1 以及配置的自定义域名）"""
        hosts = ["localhost", "127.0.0.1"]
        if self.allowed_host_keyword and self.allowed_host_keyword != "localhost":
            clean_host = self.allowed_host_keyword.split("//")[-1].split("/")[0].split(":")[0].strip()
            if clean_host not in hosts:
                hosts.append(clean_host)
        
        # 自动包含用户配置的自定义域名
        custom_cf = getattr(config, "cloudflare_custom_hostname", "").strip()
        if custom_cf:
            h = custom_cf.split("//")[-1].split("/")[0].split(":")[0].strip()
            if h and h not in hosts:
                hosts.append(h)

        custom_ng = getattr(config, "ngrok_custom_domain", "").strip()
        if custom_ng:
            h = custom_ng.split("//")[-1].split("/")[0].split(":")[0].strip()
            if h and h not in hosts:
                hosts.append(h)
        
        # 兜底：如果允许列表只有 localhost，动态向 ngrok 4040 API 查询当前激活的公网域名
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=1) as resp:
                data = json.load(resp)
                for t in data.get("tunnels", []):
                    purl = t.get("public_url", "")
                    if purl:
                        h = purl.split("//")[-1].split("/")[0].split(":")[0].strip()
                        if h and h not in hosts:
                            hosts.append(h)
                            self.allowed_host_keyword = h
        except Exception:
            pass
        return hosts

    def _test_public_url(self, url: str) -> bool:
        """测试公网 URL 是否可达且后端在线"""
        logger.debug(f"Testing public URL reachability: {url}")
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'MailAgent/1.0', 'ngrok-skip-browser-warning': '1'}
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                if resp.status < 400:
                    logger.info(f"✅ Tunnel public endpoint is reachable (HTTP {resp.status}).")
                    return True
            return True
        except HTTPError as e:
            err_body = e.read().decode('utf-8', errors='ignore')
            # 检查是否为 ngrok 边缘节点离线错误 (ERR_NGROK_3200)
            if "ERR_NGROK_" in err_body or "is offline" in err_body:
                logger.warning(f"⚠️ Tunnel endpoint is offline on ngrok edge ({e.code} - {err_body[:120].strip()})")
                return False
            # 其他 HTTP 状态（400/403/404/405 等来自本地服务的响应）表示隧道连通
            logger.info(f"✅ Tunnel public endpoint responded with HTTP {e.code}.")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Tunnel public endpoint test failed: {e}")
            return False

    def ensure_ngrok_running(self) -> str:
        logger.debug("Checking ngrok status...")
        ngrok_api_url = "http://127.0.0.1:4040/api/tunnels"
        
        # 1. 检查已有 ngrok 隧道
        try:
            with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                data = json.load(response)
                tunnels = data.get("tunnels", [])
                for tunnel in tunnels:
                    addr = tunnel.get("config", {}).get("addr", "")
                    if str(self.port) in addr or len(tunnels) == 1:
                        public_url = tunnel.get("public_url")
                        if public_url:
                            # 验证隧道是否真正连通（防止陈旧的离线 session）
                            if self._test_public_url(public_url):
                                logger.info(f"ℹ️ Found active verified ngrok tunnel: {public_url}")
                                return public_url
                            else:
                                logger.warning(f"⚠️ Existing ngrok tunnel '{public_url}' is dead/offline. Terminating stale ngrok...")
                                self._kill_ngrok_process()
                                time.sleep(1.5)
                                break
        except URLError:
            logger.debug("ngrok API not reachable. Starting fresh ngrok...")

        # 2. 启动新 ngrok 进程
        try:
            import tempfile
            log_file_path = os.path.join(tempfile.gettempdir(), "ngrok_tunnel.log")
            if os.path.exists(log_file_path):
                try:
                    os.remove(log_file_path)
                except Exception:
                    pass
            log_file = open(log_file_path, "w", encoding="utf-8", errors="replace")
            
            custom_domain = getattr(config, "ngrok_custom_domain", "").strip()
            cmd = ["ngrok", "http", str(self.port)]
            if custom_domain:
                cmd.extend(["--domain", custom_domain])
            cmd.extend(["--log", "stdout"])

            self.ngrok_process = subprocess.Popen(
                cmd, 
                shell=True, 
                stdout=log_file, 
                stderr=log_file
            )
            logger.info(f"🚀 Started ngrok http {self.port}{' (domain: ' + custom_domain + ')' if custom_domain else ''} (PID: {self.ngrok_process.pid})")
            
            logger.debug("Waiting for ngrok to initialize...")
            for _ in range(12):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                        data = json.load(response)
                        if data.get("tunnels"):
                            public_url = data["tunnels"][0].get("public_url")
                            logger.info(f"✅ ngrok started successfully. Public URL: {public_url}")
                            logger.info(f"🔗 Notion Buttons Webhook endpoint: {public_url}/?action=reply_all")
                            return public_url
                except Exception:
                    continue

            # 检查 ngrok 是否输出错误日志
            if os.path.exists(log_file_path):
                with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                    err_content = f.read().strip()
                    if err_content:
                        # 查找关键错误行
                        for line in err_content.splitlines():
                            if "ERR_NGROK_" in line or "error" in line.lower():
                                logger.warning(f"⚠️ ngrok diagnostic: {line.strip()[:180]}")
                                break
        except Exception as e:
            logger.warning(f"⚠️ Failed to start ngrok: {e}")
        
        return ""

    def ensure_cloudflare_running(self) -> str:
        token = getattr(config, "cloudflare_tunnel_token", "").strip()
        custom_hostname = getattr(config, "cloudflare_custom_hostname", "").strip()

        # 检查系统是否已有正在运行的 cloudflared.exe（如 Windows Service）
        is_already_running = False
        if sys.platform == "win32":
            try:
                out = subprocess.check_output('tasklist /FI "IMAGENAME eq cloudflared.exe" /NH', shell=True).decode('utf-8', errors='ignore')
                if "cloudflared.exe" in out:
                    is_already_running = True
            except Exception:
                pass

        # 1. 优先使用 Cloudflare Named Tunnel (Token 模式，获得固定域名)
        if token:
            if is_already_running:
                logger.info("ℹ️ Detected existing cloudflared process / Windows Service already running.")
            else:
                logger.info("🔒 Starting Cloudflare Named Tunnel using configured Tunnel Token...")
                try:
                    self.cloudflared_process = subprocess.Popen(
                        ["cloudflared", "tunnel", "run", "--token", token],
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    logger.info(f"🚀 Started cloudflared named tunnel with Token (PID: {self.cloudflared_process.pid})")
                except Exception as e:
                    logger.error(f"❌ Failed to start cloudflared with token: {e}")
                    return ""
            
            if custom_hostname:
                if not custom_hostname.startswith("http"):
                    custom_hostname = f"https://{custom_hostname}"
                logger.info(f"✅ Cloudflare Tunnel connected to fixed domain: {custom_hostname}")
                logger.info(f"🔗 Notion Buttons Webhook endpoint: {custom_hostname}/?action=reply_all")
                return custom_hostname
            else:
                logger.info("ℹ️ Cloudflare Tunnel Token active. (Tip: Enter 'Custom Cloudflare domain / URL' in Settings to display the full webhook link).")
                return "https://cloudflare-tunnel-active"

        # 2. 用户指定了自定义 URL (例如已有独立运行的 cloudflared 服务或自定义反代)
        if custom_hostname:
            if not custom_hostname.startswith("http"):
                custom_hostname = f"https://{custom_hostname}"
            logger.info(f"ℹ️ Custom Cloudflare URL configured: {custom_hostname}")
            try:
                import tempfile
                log_file_path = os.path.join(tempfile.gettempdir(), "cloudflared_quick_tunnel.log")
                log_file = open(log_file_path, "w", encoding="utf-8")
                self.cloudflared_process = subprocess.Popen(
                    ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{self.port}"],
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file
                )
                logger.info(f"🚀 Started cloudflared daemon (PID: {self.cloudflared_process.pid})")
            except Exception as e:
                logger.warning(f"⚠️ Could not launch local cloudflared process: {e}")
            logger.info(f"✅ Active Cloudflare URL: {custom_hostname}")
            logger.info(f"🔗 Notion Buttons Webhook endpoint: {custom_hostname}/?action=reply_all")
            return custom_hostname

        # 3. 回退到 Cloudflare Quick Tunnel (免登录随机域名模式)
        logger.debug("Checking cloudflared quick tunnel status...")
        try:
            import tempfile
            import re
            log_file_path = os.path.join(tempfile.gettempdir(), "cloudflared_quick_tunnel.log")
            if os.path.exists(log_file_path):
                try:
                    os.remove(log_file_path)
                except Exception:
                    pass
            
            log_file = open(log_file_path, "w", encoding="utf-8")
            self.cloudflared_process = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{self.port}"],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=log_file
            )
            logger.info(f"🚀 Started cloudflared quick tunnel (PID: {self.cloudflared_process.pid})")
            
            logger.debug("Waiting for cloudflared tunnel to initialize...")
            for _ in range(20):
                time.sleep(1)
                if os.path.exists(log_file_path):
                    with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                        matches = re.findall(r'https://([a-zA-Z0-9-]+)\.trycloudflare\.com', content)
                        for m in matches:
                            if m.lower() != 'api':
                                public_url = f"https://{m}.trycloudflare.com"
                                logger.info(f"✅ cloudflared started successfully. Public URL: {public_url}")
                                logger.info(f"🔗 Notion Buttons Webhook endpoint: {public_url}/?action=reply_all")
                                return public_url
        except Exception as e:
            logger.error(f"❌ Failed to start cloudflared: {e}")
        return ""

    def init_tunnel(self) -> str:
        """初始化隧道、打印公网 Webhook URL 并执行自检"""
        provider = getattr(config, "reverse_proxy", "").lower().strip()
        public_url = ""
        
        logger.info(f"🌐 [Reverse Proxy] Initializing tunnel provider: '{provider}' on local port {self.port}...")
        
        if provider == "cloudflare":
            public_url = self.ensure_cloudflare_running()
        elif provider == "ngrok":
            public_url = self.ensure_ngrok_running()
        elif provider == "" or provider == "none":
            logger.info("ℹ️ REVERSE_PROXY is disabled in Settings. Tunnel will not be started.")
            return "localhost"
        else:
            logger.warning(f"⚠️ Unknown REVERSE_PROXY provider: '{provider}'. Skipping tunnel.")
            return "localhost"

        if public_url:
            specific_host = public_url.split("//")[-1]
            self.allowed_host_keyword = specific_host
            
            logger.info("=" * 70)
            logger.info(f"🚀 [Reverse Proxy / Webhook] Active Provider: {provider.upper()}")
            logger.info(f"🌐 Public Tunnel URL: {public_url}")
            logger.info(f"🔗 Notion Buttons Webhook Endpoint: {public_url}/?action=reply_all")
            logger.info(f"🔒 Security Host Filter: '{self.allowed_host_keyword}'")
            logger.info("=" * 70)
            
            def self_check():
                import urllib.request
                import json
                import ssl
                
                # 给边缘节点（尤其是 Cloudflare Quick Tunnel / ngrok）5 秒初始化和 DNS 传播时间
                time.sleep(5.0)
                max_retries = 5
                for attempt in range(1, max_retries + 1):
                    logger.info(f"🔍 [Tunnel Self-Check] Probing public endpoint (Attempt {attempt}/{max_retries}): {public_url}?action=ping ...")
                    try:
                        req = urllib.request.Request(f"{public_url}?action=ping", method="POST")
                        req.add_header("Content-Type", "application/json")
                        req.add_header("ngrok-skip-browser-warning", "1")
                        req.add_header("User-Agent", "MailAgent-SelfCheck/1.0")
                        data = json.dumps({"type": "ping"}).encode("utf-8")
                        
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                        
                        with urllib.request.urlopen(req, data=data, timeout=8, context=ctx) as resp:
                            if resp.status == 200:
                                resp_body = resp.read().decode("utf-8", errors="ignore").strip()
                                logger.info(f"✅ [Tunnel Self-Check] PASSED (HTTP {resp.status} - {resp_body})! Webhook is verified reachable from public internet.")
                                return
                            else:
                                logger.debug(f"ℹ️ [Tunnel Self-Check] Returned HTTP status: {resp.status}")
                    except HTTPError as e:
                        err_body = e.read().decode('utf-8', errors='ignore')
                        # 502/530 为 Cloudflare/边缘节点初始连接中的常见状态，继续等待重试
                        if e.code in (502, 530, 504):
                            logger.debug(f"⏳ [Tunnel Self-Check] Edge warming up (HTTP {e.code}), retrying...")
                        else:
                            logger.debug(f"ℹ️ [Tunnel Self-Check] HTTP {e.code}: {err_body[:100]}")
                    except Exception as e:
                        logger.debug(f"⏳ [Tunnel Self-Check] Probe attempt {attempt}: {e}")
                    
                    time.sleep(4.0)
                
                logger.info(f"ℹ️ [Tunnel Self-Check] Initial probing finished. Tunnel '{public_url}' remains active in background.")
                    
            import threading
            threading.Thread(target=self_check, daemon=True, name="TunnelSelfCheck").start()
            return self.allowed_host_keyword
        else:
            logger.error(f"❌ [Reverse Proxy] Failed to establish tunnel or obtain public URL from '{provider}'.")
            logger.error(f"💡 Email synchronization will continue normally.")
            return "localhost"

    def _kill_ngrok_process(self):
        """彻底清理 ngrok 进程"""
        if self.ngrok_process:
            try:
                self.ngrok_process.terminate()
                try:
                    self.ngrok_process.wait(timeout=2)
                except Exception:
                    self.ngrok_process.kill()
            except Exception:
                pass
            self.ngrok_process = None
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/IM", "ngrok.exe"], capture_output=True)
            except Exception:
                pass
        else:
            try:
                subprocess.run(["pkill", "-f", "ngrok"], capture_output=True)
            except Exception:
                pass

    def _kill_cloudflared_process(self):
        """仅清理 MailAgent 自身启动的 cloudflared 子进程，不误杀系统独立运行的 cloudflared 服务"""
        if self.cloudflared_process:
            try:
                pid = self.cloudflared_process.pid
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                else:
                    self.cloudflared_process.terminate()
            except Exception:
                pass
            self.cloudflared_process = None

    def stop_all(self):
        """停止所有由 MailAgent 启动的隧道子进程"""
        logger.info("🛑 Stopping active tunnel sub-processes...")
        self._kill_ngrok_process()
        self._kill_cloudflared_process()


global_tunnel_manager = TunnelManager()

