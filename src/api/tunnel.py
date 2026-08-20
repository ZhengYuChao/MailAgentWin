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
        self.allowed_host_keyword = "localhost"

    def _test_public_url(self, url: str) -> bool:
        """测试公网 URL 是否可达。本地 API Server 未启动时预期返回 HTTPError(5xx)"""
        logger.debug(f"Testing public URL reachability: {url}")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'MailAgent/1.0'})
            with urllib.request.urlopen(req, timeout=5):
                pass
            logger.info("✅ Tunnel public endpoint is reachable (HTTP 200).")
            return True
        except urllib.error.HTTPError as e:
            # 只要是 HTTPError，说明 ngrok 边缘节点接收了请求并返回了错误（如 502），隧道公网连通性没问题
            logger.info(f"✅ Tunnel public endpoint is reachable (ngrok responded HTTP {e.code}).")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Tunnel public endpoint test failed: {e}")
            return False

    def ensure_ngrok_running(self) -> str:
        logger.debug("Checking ngrok status...")
        ngrok_api_url = "http://127.0.0.1:4040/api/tunnels"
        target_addr = f"localhost:{self.port}"
        
        try:
            with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                data = json.load(response)
                for tunnel in data.get("tunnels", []):
                    addr = tunnel.get("config", {}).get("addr", "")
                    if target_addr in addr:
                        public_url = tunnel.get("public_url")
                        logger.info(f"ℹ️ Found existing ngrok tunnel: {public_url}")
                        
                        if self._test_public_url(public_url):
                            return public_url
                        else:
                            logger.warning("⚠️ Existing ngrok tunnel is dead. Killing old ngrok process...")
                            subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe"], capture_output=True)
                            time.sleep(1)
                            break
        except URLError:
            logger.debug("ngrok API not reachable. Attempting to start ngrok...")

        try:
            self.ngrok_process = subprocess.Popen(
                ["ngrok", "http", str(self.port)], 
                shell=True, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
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
                            self._test_public_url(public_url)
                            return public_url
                except URLError:
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
                            return public_url
        except Exception as e:
            logger.error(f"❌ Failed to start cloudflared: {e}")
        return ""

    def init_tunnel(self) -> str:
        """初始化隧道并返回允许的 host keyword"""
        provider = getattr(config, "reverse_proxy", "").lower()
        public_url = ""
        
        if provider == "cloudflare":
            public_url = self.ensure_cloudflare_running()
        elif provider == "ngrok":
            public_url = self.ensure_ngrok_running()
        elif provider == "":
            logger.info("ℹ️ REVERSE_PROXY not configured, skip launching reverse proxy tunnel.")
            return "localhost"
        else:
            logger.warning(f"⚠️ Unknown REVERSE_PROXY provider: {provider}")
            return "localhost"

        if public_url:
            specific_host = public_url.split("//")[-1]
            self.allowed_host_keyword = specific_host
            logger.info(f"🔒 Security: Only accepting requests with Host: '{self.allowed_host_keyword}'")
            
            def self_check():
                import time
                import urllib.request
                import json
                
                max_retries = 3
                for attempt in range(1, max_retries + 1):
                    time.sleep(3)
                    logger.info(f"🔍 Running tunnel self-check (Attempt {attempt}/{max_retries})...")
                    try:
                        req = urllib.request.Request(f"{public_url}?action=ping", method="POST")
                        req.add_header("Content-Type", "application/json")
                        req.add_header("ngrok-skip-browser-warning", "1")
                        req.add_header("User-Agent", "Mozilla/5.0")
                        data = json.dumps({"type": "ping"}).encode("utf-8")
                        
                        import ssl
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                        
                        with urllib.request.urlopen(req, data=data, timeout=10, context=ctx) as resp:
                            if resp.status == 200:
                                logger.info("✅ Tunnel self-check passed. Webhook is reachable from public internet.")
                                return
                            else:
                                logger.warning(f"⚠️ Tunnel self-check returned status: {resp.status}")
                    except urllib.error.HTTPError as e:
                        err_body = e.read().decode('utf-8', errors='ignore')
                        logger.error(f"❌ Tunnel self-check failed on attempt {attempt}: HTTP {e.code} - {err_body[:200]}")
                        if "ERR_NGROK_3200" in err_body or "ERR_NGROK_" in err_body:
                            logger.error("⚠️ Detected ngrok specific error, breaking retries early to restart tunnel.")
                            break
                    except Exception as e:
                        logger.error(f"❌ Tunnel self-check failed on attempt {attempt}: {e}")
                
                logger.error("❌ Tunnel self-check completely failed. Restarting tunnel...")
                self.stop_all()
                import time
                time.sleep(2)
                self.init_tunnel()
                    
            import threading
            threading.Thread(target=self_check, daemon=True).start()

            return self.allowed_host_keyword
            
        return "localhost"

    def stop_all(self):
        """停止所有隧道进程"""
        if self.ngrok_process:
            logger.info(f"🛑 Killing ngrok process (PID: {self.ngrok_process.pid})...")
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.ngrok_process.pid)], capture_output=True)
                self.ngrok_process = None
            except Exception as e:
                logger.error(f"❌ Failed to kill ngrok: {e}")
                
        if self.cloudflared_process:
            logger.info(f"🛑 Killing cloudflared process (PID: {self.cloudflared_process.pid})...")
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.cloudflared_process.pid)], capture_output=True)
                self.cloudflared_process = None
            except Exception as e:
                logger.error(f"❌ Failed to kill cloudflared: {e}")

global_tunnel_manager = TunnelManager()
