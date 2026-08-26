"""
CalendarWorker 后台任务

定期从 Outlook 日历文件夹中读取事件并同步到 Notion 日历库。
设计为在 MailWorker 进程中作为 async task 运行。

Usage:
    from workers.calendar_worker import start_calendar_sync
    asyncio.create_task(start_calendar_sync(shutdown_event))
"""

import asyncio
from typing import Set
from loguru import logger
from multiprocessing.synchronize import Event as MPEvent

from src.config import config
from src.notion.calendar_sync import NotionCalendarSync
from src.mail.outlook_calendar_reader import OutlookCalendarReader
from src.runtime.status_registry import registry


async def start_calendar_sync(shutdown_event: MPEvent):
    """Calendar sync main loop with status registry reporting."""
    if not config.calendar_database_id:
        logger.info("ℹ️ CALENDAR_DATABASE_ID not set, Calendar Sync Worker is disabled.")
        registry.update("calendar", status="off", task="Not configured", reason="")
        return

    logger.info("🚀 Starting Calendar Sync Worker...")
    registry.update("calendar", status="starting", task="Initializing")


    sync = NotionCalendarSync()
    reader = OutlookCalendarReader()

    interval = config.calendar_check_interval
    past_days = config.calendar_past_days
    future_days = config.calendar_future_days

    # 记录已处理过的 event_ids（简单内存去重）
    processed_ids: Set[str] = set()

    try:
        # 初次启动时，从 Notion 获取已有的所有 Event ID 避免全量重复更新
        logger.info("📅 Initializing calendar sync, fetching existing event IDs...")
        processed_ids = await sync.query_all_event_ids()
        logger.info(f"📅 Found {len(processed_ids)} existing calendar events in Notion.")
        registry.update("calendar", status="normal", task="Idle")

        while not shutdown_event.is_set():
            try:
                # 1. 从 Outlook 读取日历事件
                registry.update("calendar", status="normal", task="Reading Outlook calendar")
                events = await asyncio.to_thread(
                    reader.read_events,
                    past_days=past_days,
                    future_days=future_days,
                )

                if events:
                    # 2. 同步到 Notion
                    # 由于 events 可能包含不需要更新的条目，这里将序列号设为 1
                    # （对于 COM 读取的，序列号机制不是非常适用，依赖于 last_modified 或直接 upsert）
                    # 对于纯 upsert，我们可以每次都去更新（NotionCalendarSync 内部会检查是否存在）
                    
                    # 过滤一下，避免频繁无效更新
                    # （如果是非常频繁更新的库，可以加更精细的 checksum 或 modified 校验）
                    success_count = 0
                    consecutive_failures = 0
                    for event in events:
                        if shutdown_event.is_set():
                            break

                        # 连续失败 3 次说明大概率是网络问题，跳过剩余事件
                        if consecutive_failures >= 3:
                            logger.warning(
                                f"⚠️ {consecutive_failures} consecutive sync failures, "
                                f"likely network issue. Skipping remaining "
                                f"{len(events) - success_count - consecutive_failures} events."
                            )
                            break

                        page_id = await sync.sync_event(event)
                        if page_id:
                            processed_ids.add(event.event_id)
                            success_count += 1
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            
                    if success_count > 0:
                        logger.debug(f"Calendar sync loop complete: {success_count} events synced.")
                
                registry.update("calendar", status="normal", task="Idle")

            except Exception as e:
                logger.error(f"❌ Error in calendar sync loop: {e}")
                registry.update("calendar", status="abnormal", reason=str(e)[:120])

            # 休眠 interval 秒，支持响应 shutdown_event
            for _ in range(interval):
                if shutdown_event.is_set():
                    break
                await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info("Calendar Sync Worker cancelled.")
    except Exception as e:
        logger.critical(f"Calendar Sync Worker crashed: {e}")
        registry.update("calendar", status="abnormal", reason=str(e)[:120])
    finally:
        await sync.close()
        logger.info("Calendar Sync Worker stopped.")
