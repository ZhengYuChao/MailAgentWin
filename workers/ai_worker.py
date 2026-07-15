"""
AIWorker (进程 B)
职责：
  - 从 ai_trigger_queue 消费邮件同步完成信号
  - 防抖 + 批次控制 + 强制间隔触发
  - Playwright 无头浏览器触发 Notion AI Chat
  - 每日定时调度 (07:00)
"""
import asyncio
import sys
from multiprocessing import Queue as MPQueue
from multiprocessing.synchronize import Event as MPEvent
from loguru import logger


def _setup_logger():
    """配置 AIWorker 进程的日志"""
    logger.remove()
    fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | [AIWorker] {message}"
    logger.add(sys.stderr, level="INFO", format=fmt)
    logger.add("logs/mailagent.log", rotation="10 MB", level="DEBUG",
               encoding="utf-8", format=fmt, enqueue=True)


def run_ai_worker(ai_trigger_queue: MPQueue, shutdown_event: MPEvent):
    """AIWorker 进程入口函数（由 ProcessManager 调用）"""
    _setup_logger()
    logger.info("=" * 50)
    logger.info("AIWorker process starting...")
    logger.info("=" * 50)

    from src.ai.controller import AIController
    from src.scheduler.daily import daily_scheduler_loop

    ai_controller = AIController(ai_trigger_queue=ai_trigger_queue, shutdown_event=shutdown_event)

    async def _run():
        # 启动防抖循环和每日定时调度
        debounce_task = asyncio.create_task(ai_controller.debounce_loop())
        daily_task = asyncio.create_task(daily_scheduler_loop(ai_controller, shutdown_event))

        try:
            # 等待关停信号
            while not shutdown_event.is_set():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("AIWorker shutting down, cancelling tasks...")
            debounce_task.cancel()
            daily_task.cancel()
            for task in [debounce_task, daily_task]:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await ai_controller.close()
            logger.info("AIWorker cleanup complete.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        logger.info("AIWorker interrupted.")
    finally:
        loop.close()
        logger.info("AIWorker stopped.")
