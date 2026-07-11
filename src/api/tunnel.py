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

    def ensure_ngrok_running(self) -> str:
        logger.info("🌐 Checking ngrok status...")
        ngrok_api_url = "http://127.0.0.1:4040/api/tunnels"
        target_addr = f"localhost:{self.port}"
        
        try:
            with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                data = json.load(response)
                for tunnel in data.get("tunnels", []):
                    addr = tunnel.get("config", {}).get("addr", "")
                    if target_addr in addr:
                        public_url = tunnel.get("public_url")
                        logger.info(f"✅ ngrok is already running. Public URL: {public_url}")
                        return public_url
        except URLError:
            logger.info("📡 ngrok API not reachable. Attempting to start ngrok...")

        try:
            self.ngrok_process = subprocess.Popen(
                ["ngrok", "http", str(self.port)], 
                shell=True, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            logger.info(f"🚀 Started ngrok http {self.port} (PID: {self.ngrok_process.pid})")
            
            logger.info("⏳ Waiting for ngrok to initialize...")
            for _ in range(10):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(ngrok_api_url, timeout=2) as response:
                        data = json.load(response)
                        if data.get("tunnels"):
                            public_url = data["tunnels"][0].get("public_url")
                            logger.info(f"✅ ngrok started successfully. Public URL: {public_url}")
                            return public_url
                except URLError:
                    continue
        except Exception as e:
            logger.error(f"❌ Failed to start ngrok: {e}")
        
        return ""

    def ensure_cloudflare_running(self) -> str:
        logger.info("🌐 Checking cloudflared status...")
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
            
            logger.info("⏳ Waiting for cloudflared tunnel to initialize...")
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
