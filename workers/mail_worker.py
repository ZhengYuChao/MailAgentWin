"""
MailWorker (进程 A)
职责：
  - Outlook COM Radar（事件订阅 + 兜底轮询）
  - Webhook HTTP Server（接收 Notion Automation 的 webhook）
  - 邮件内容获取 → Notion 同步
  - 历史补查
  - Draft 发送/保存 (COM 操作)
  - 每次邮件同步成功后，通过 ai_trigger_queue 通知 AIWorker
"""
import asyncio
import sys
import threading
from multiprocessing import Queue as MPQueue
from multiprocessing.synchronize import Event as MPEvent
from loguru import logger


def _setup_logger():
    """配置 MailWorker 进程的日志"""
    logger.remove()
    fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | [MailWorker] {message}"
    logger.add(sys.stderr, level="INFO", format=fmt)
    logger.add("logs/mailagent.log", rotation="10 MB", level="DEBUG",
               encoding="utf-8", format=fmt, enqueue=True)


def run_mail_worker(ai_trigger_queue: MPQueue, shutdown_event: MPEvent):
    """MailWorker 进程入口函数（由 ProcessManager 调用）"""
    _setup_logger()
    logger.info("=" * 50)
    logger.info("MailWorker process starting...")
    logger.info("=" * 50)

    from src.config import config
    from src.mail.new_watcher_win import WindowsWatcher
    from src.api.tunnel import global_tunnel_manager
    from src.api.server import start_api_server

    async def _run():
        # 1. 启动内网穿透隧道及 API Server (按需)
        provider = getattr(config, "reverse_proxy", "").strip()
        if provider:
            logger.info(f"⚙️ REVERSE_PROXY enabled ('{provider}'). Starting Tunnel and API Server...")
            global_tunnel_manager.init_tunnel()
            api_thread = threading.Thread(target=start_api_server, daemon=True, name="APIServer")
            api_thread.start()
        else:
            logger.info("ℹ️ REVERSE_PROXY disabled. API Server and Tunnel will not be started.")

        # 2. 启动 Watcher（传入 IPC 队列和关停事件）
        watcher = WindowsWatcher(ai_trigger_queue=ai_trigger_queue, shutdown_event=shutdown_event)
        try:
            await watcher.start()
        except asyncio.CancelledError:
            logger.info("MailWorker run cancelled.")
        except Exception as e:
            logger.critical(f"MailWorker crashed: {e}")
            raise
        finally:
            await watcher.stop()
            global_tunnel_manager.stop_all()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        logger.info("MailWorker interrupted.")
    finally:
        loop.close()
        logger.info("MailWorker stopped.")
