import asyncio
import datetime
from loguru import logger
from src.scheduler.task_pool import global_task_pool
from src.models import TaskType, TaskPriority

async def daily_scheduler_loop():
    """Daily scheduler loop: triggers a chat automation at 07:00 every day."""
    logger.info("⏰ Daily scheduler loop started (Target: 07:00 AM).")
    
    last_triggered_date = None
    
    while True:
        try:
            now = datetime.datetime.now()
            if now.hour == 7 and now.minute == 0:
                if last_triggered_date != now.date():
                    logger.info("🔔 Daily scheduled trigger (07:00) reached. Enqueuing task...")
                    
                    payload = {
                        "subject": "Daily Morning Sync"
                    }
                    # 放入 TaskPool，中低优先级即可，这只是触发无头浏览器
                    global_task_pool.add_task(TaskType.DAILY_SCHEDULE, TaskPriority.MEDIUM, payload)
                    
                    last_triggered_date = now.date()
            
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ Scheduler error: {e}")
            await asyncio.sleep(60)
