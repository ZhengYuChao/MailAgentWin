# notion_auth.py
import asyncio
import os
from playwright.async_api import async_playwright

# 获取脚本同级目录下的绝对路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_AGENT_PATH = os.path.join(SCRIPT_DIR, "user_agent.txt")
AUTH_STATE_PATH = os.path.join(SCRIPT_DIR, "notion_auth.json")

async def run():
    async with async_playwright() as p:
        # 启动有头模式
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        # 自动获取当前浏览器的 User-Agent 并保存
        user_agent = await page.evaluate("navigator.userAgent")
        with open(USER_AGENT_PATH, "w", encoding="utf-8") as f:
            f.write(user_agent)
        print(f"✅ Automatically extracted and saved User-Agent: {user_agent}")
        print(f"   Path: {USER_AGENT_PATH}")
        
        # 跳转登录
        await page.goto("https://www.notion.so/login", timeout=120000)
        
        print("\n=====================================================")
        print("Please complete your Notion login in the popup browser window...")
        print("Once logged in successfully and your Notion workspace has loaded, return here and press [Enter] to continue...")
        print("=====================================================\n")
        
        input()  # 等待在终端敲击回车
        
        # 保存登录状态（Cookies 和 LocalStorage）
        await context.storage_state(path=AUTH_STATE_PATH)
        print("✅ Login state saved successfully to notion_auth.json!")
        print(f"   Path: {AUTH_STATE_PATH}")
        print("🎯 Setup completed! Future headless browsers will load these authentication files.")
        
        await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
