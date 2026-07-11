import asyncio
import sys
import threading
from loguru import logger
from src.config import config

# 配置日志
logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("logs/mailagent.log", rotation="10 MB", level="DEBUG", encoding="utf-8")

from src.mail.new_watcher_win import WindowsWatcher
from src.ai.controller import global_ai_controller
from src.scheduler.daily import daily_scheduler_loop
from src.api.tunnel import global_tunnel_manager
from src.api.server import start_api_server

async def run_agent():
    # 1. 检查并启动内网穿透隧道及 API Server (按需解耦启动)
    provider = getattr(config, "reverse_proxy", "").strip()
    if provider:
        logger.info(f"⚙️ REVERSE_PROXY is enabled ('{provider}'). Initializing Tunnel and API Server...")
        global_tunnel_manager.init_tunnel()
        # 启动 HTTP Server 线程
        api_thread = threading.Thread(target=start_api_server, daemon=True, name="APIServer")
        api_thread.start()
    else:
        logger.info("ℹ️ REVERSE_PROXY is disabled. API Server and Tunnel will not be started.")

    # 2. 启动防抖和调度任务
    debounce_task = asyncio.create_task(global_ai_controller.debounce_loop())
    daily_task = asyncio.create_task(daily_scheduler_loop())

    # 3. 启动中央任务循环 (Watcher)
    watcher = WindowsWatcher()
    try:
        await watcher.start()
    except asyncio.CancelledError:
        logger.info("Agent run cancelled.")
    except Exception as e:
        logger.critical(f"MailAgent crashed: {e}")
        raise
    finally:
        await watcher.stop()
        debounce_task.cancel()
        daily_task.cancel()
        global_tunnel_manager.stop_all()

if __name__ == "__main__":
    logger.info("MailAgent Windows Service Starting...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    main_task = loop.create_task(run_agent())
    
    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        logger.info("Interrupt received, shutting down...")
        main_task.cancel()
        try:
            loop.run_until_complete(main_task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.close()
        logger.info("MailAgent stopped.")
