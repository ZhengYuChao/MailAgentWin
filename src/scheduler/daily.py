import asyncio
import datetime
from loguru import logger


async def daily_scheduler_loop(ai_controller=None, shutdown_event=None):
    """Daily scheduler loop: triggers a chat automation at 07:00 every day.

    在进程 B (AIWorker) 中运行，直接调用 ai_controller 触发 Notion AI Chat。

    Args:
        ai_controller: AIController 实例，用于触发 Notion AI Chat
        shutdown_event: multiprocessing.Event，用于检测关停信号
    """
    logger.info("⏰ Daily scheduler loop started (Target: 07:00 AM).")

    last_triggered_date = None

    while True:
        try:
            # 检查关停信号
            if shutdown_event and shutdown_event.is_set():
                logger.info("Shutdown event detected, stopping daily scheduler.")
                break

            now = datetime.datetime.now()
            if now.hour == 7 and now.minute == 0:
                if last_triggered_date != now.date():
                    logger.info("🔔 Daily scheduled trigger (07:00) reached. Triggering Notion AI...")

                    if ai_controller is not None:
                        await ai_controller.execute_ai_trigger("Daily Schedule", action="scheduled_daily_sync")
                    else:
                        logger.warning("⚠️ ai_controller is None, skipping daily trigger.")

                    last_triggered_date = now.date()

            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ Scheduler error: {e}")
            await asyncio.sleep(60)
