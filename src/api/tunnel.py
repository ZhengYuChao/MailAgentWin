import os
import time
import json
import subprocess
import urllib.request
from urllib.error import URLError
from loguru import logger
from src.config import config

class TunnelManager:
    """管理 ngrok 或 cloudflared 隧道"""
    
    def __init__(self, port: int = 54321):
        self.port = port
        self.ngrok_process = None
        self.cloudflared_process = None
    def get_allowed_hosts(self) -> list[str]:
        """返回所有允许的 Host 列表（包括 localhost, 127.0.0.1 以及动态探测到的 ngrok/cloudflare 域名）"""
        hosts = ["localhost", "127.0.0.1"]
        if self.allowed_host_keyword and self.allowed_host_keyword != "localhost":
            clean_host = self.allowed_host_keyword.split("//")[-1].split(":")[0].strip()
            if clean_host not in hosts:
                hosts.append(clean_host)
        
        # 兜底：如果允许列表只有 localhost，动态向 ngrok 4040 API 查询当前激活的公网域名
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=1) as resp:
                data = json.load(resp)
                for t in data.get("tunnels", []):
                    purl = t.get("public_url", "")
                    if purl:
                        h = purl.split("//")[-1].split(":")[0].strip()
                        if h and h not in hosts:
                            hosts.append(h)
                            self.allowed_host_keyword = h
        except Exception:
            pass
        return hosts

    def _test_public_url(self, url: str) -> bool:
        """测试公网 URL 是否可达"""
        logger.debug(f"Testing public URL reachability: {url}")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'MailAgent/1.0', 'ngrok-skip-browser-warning': '1'})
            with urllib.request.urlopen(req, timeout=5):
                pass
            logger.info("✅ Tunnel public endpoint is reachable (HTTP 200).")
            return True
        except urllib.error.HTTPError as e:
            # 只要是 HTTPError，说明 ngrok 边缘节点接收了请求并返回了响应（如 404/502），隧道连通性没问题
            logger.info(f"✅ Tunnel public endpoint is reachable (ngrok responded HTTP {e.code}).")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Tunnel public endpoint test failed: {e}")
            return False

    def ensure_ngrok_running(self) -> str:
        logger.debug("Checking ngrok status...")
        ngrok_api_url = "http://127.0.0.1:4040/api/tunnels"
        
        try:
            with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                data = json.load(response)
                tunnels = data.get("tunnels", [])
                for tunnel in tunnels:
                    addr = tunnel.get("config", {}).get("addr", "")
                    if str(self.port) in addr or len(tunnels) == 1:
                        public_url = tunnel.get("public_url")
                        if public_url:
                            logger.info(f"ℹ️ Found existing active ngrok tunnel: {public_url}")
                            return public_url
        except URLError:
            logger.debug("ngrok API not reachable. Attempting to start ngrok...")

        try:
            import tempfile
            log_file_path = os.path.join(tempfile.gettempdir(), "ngrok_tunnel.log")
            if os.path.exists(log_file_path):
                try:
                    os.remove(log_file_path)
                except Exception:
                    pass
            log_file = open(log_file_path, "w", encoding="utf-8", errors="replace")
            self.ngrok_process = subprocess.Popen(
                ["ngrok", "http", str(self.port), "--log", "stdout"], 
                shell=True, 
                stdout=log_file, 
                stderr=log_file
            )
            logger.info(f"🚀 Started ngrok http {self.port} (PID: {self.ngrok_process.pid})")
            
            logger.debug("Waiting for ngrok to initialize...")
            for _ in range(10):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                        data = json.load(response)
                        if data.get("tunnels"):
                            public_url = data["tunnels"][0].get("public_url")
                            logger.info(f"✅ ngrok started successfully. Public URL: {public_url}")
                            logger.info(f"🔗 Notion Buttons Webhook endpoint: {public_url}/?action=reply_all")
                            self._test_public_url(public_url)
                            return public_url
                except URLError:
                    if self.ngrok_process.poll() is not None:
                        if os.path.exists(log_file_path):
                            with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                                err_content = f.read()
                                logger.error(f"❌ ngrok exited early with code {self.ngrok_process.returncode}: {err_content.strip()[:300]}")
                        break
                    continue
        except Exception as e:
            logger.error(f"❌ Failed to start ngrok: {e}")
        
        return ""

    def ensure_cloudflare_running(self) -> str:
        logger.debug("Checking cloudflared status...")
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
            for _ in range(15):
                time.sleep(1)
                if os.path.exists(log_file_path):
                    with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', content)
                        if match:
                            public_url = match.group(0)
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
        elif provider == "":
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
                
                max_retries = 5
                for attempt in range(1, max_retries + 1):
                    time.sleep(2.5)
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
                        
                        with urllib.request.urlopen(req, data=data, timeout=10, context=ctx) as resp:
                            if resp.status == 200:
                                resp_body = resp.read().decode("utf-8", errors="ignore").strip()
                                logger.info(f"✅ [Tunnel Self-Check] PASSED (HTTP {resp.status} - {resp_body})! Webhook is verified reachable from public internet.")
                                return
                            else:
                                logger.warning(f"⚠️ [Tunnel Self-Check] Returned HTTP status: {resp.status}")
                    except urllib.error.HTTPError as e:
                        err_body = e.read().decode('utf-8', errors='ignore')
                        logger.error(f"❌ [Tunnel Self-Check] HTTP Error on attempt {attempt}: HTTP {e.code} - {err_body[:200]}")
                        if "ERR_NGROK_3200" in err_body or "ERR_NGROK_" in err_body:
                            logger.error("⚠️ Detected ngrok specific error, breaking retries early to restart tunnel.")
                            break
                    except Exception as e:
                        logger.error(f"❌ [Tunnel Self-Check] Connection failed on attempt {attempt}: {e}")
                
                logger.error("❌ [Tunnel Self-Check] Completely failed after retries. Restarting tunnel...")
                self.stop_all()
                time.sleep(2)
                self.init_tunnel()
                    
            import threading
            threading.Thread(target=self_check, daemon=True, name="TunnelSelfCheck").start()
            return self.allowed_host_keyword
        else:
            logger.error(f"❌ [Reverse Proxy] Failed to establish tunnel or obtain public URL from '{provider}'.")
            logger.error(f"💡 Please verify that '{provider}' executable is installed and available in system PATH.")
            return "localhost"

    def stop_all(self):
        """停止所有隧道进程"""
        if self.ngrok_process:
            logger.info(f"🛑 Terminating ngrok process (PID: {self.ngrok_process.pid})...")
            try:
                self.ngrok_process.terminate()
                try:
                    self.ngrok_process.wait(timeout=2)
                except Exception:
                    self.ngrok_process.kill()
            except Exception as e:
                logger.error(f"❌ Failed to terminate ngrok: {e}")
            self.ngrok_process = None
                
        if self.cloudflared_process:
            logger.info(f"🛑 Terminating cloudflared process (PID: {self.cloudflared_process.pid})...")
            try:
                self.cloudflared_process.terminate()
                try:
                    self.cloudflared_process.wait(timeout=2)
                except Exception:
                    self.cloudflared_process.kill()
            except Exception as e:
                logger.error(f"❌ Failed to terminate cloudflared: {e}")
            self.cloudflared_process = None

global_tunnel_manager = TunnelManager()
