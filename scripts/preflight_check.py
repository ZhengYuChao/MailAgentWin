# scripts/preflight_check.py
import sys
import os
from pathlib import Path

# 将项目根目录添加到 sys.path
root_dir = Path(__file__).parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import requests
import win32com.client
import pythoncom
from loguru import logger

def check_outlook():
    try:
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        accounts = [a.DisplayName for a in ns.Accounts]
        logger.info(f"✅ Outlook OK, accounts={accounts}")
        return True
    except Exception as e:
        logger.error(f"❌ Outlook unreachable: {e}")
        return False
    finally:
        pythoncom.CoUninitialize()

def check_notion(token):
    try:
        r = requests.get("https://api.notion.com/v1/users/me",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=5)
        if r.ok:
            logger.info(f"✅ Notion API OK: {r.status_code}")
        else:
            logger.error(f"❌ Notion API Failed: {r.status_code} - {r.text}")
        return r.ok
    except Exception as e:
        logger.error(f"❌ Notion API Error: {e}")
        return False

def check_feishu(webhook):
    if not webhook:
        logger.warning("⚠️ Feishu webhook not configured, skipping check.")
        return True
    try:
        r = requests.post(webhook, json={"msg_type": "text", "content": {"text": "MailAgent Windows Preflight Check"}}, timeout=5)
        if r.ok:
            logger.info(f"✅ Feishu webhook OK: {r.status_code}")
        else:
            logger.error(f"❌ Feishu webhook Failed: {r.status_code}")
        return r.ok
    except Exception as e:
        logger.error(f"❌ Feishu webhook Error: {e}")
        return False

if __name__ == "__main__":
    from src.config import config
    
    logger.info("Starting Preflight Check...")
    
    ok = all([
        check_outlook(),
        check_notion(config.notion_token),
        check_feishu(config.feishu_webhook_url)
    ])
    
    if ok:
        logger.info("🚀 All systems GO!")
    else:
        logger.error("🚨 Some checks failed. Please check your configuration.")
        sys.exit(1)
