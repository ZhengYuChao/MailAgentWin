import asyncio
import os
import time
from typing import Optional
from loguru import logger
from datetime import datetime, timezone, timedelta

from src.mail.outlook_com_arm import OutlookComArm, OL_FOLDER_SENT, OL_FOLDER_INBOX
from src.mail.com_radar import start_radar
from src.mail.sync_store import SyncStore
from src.mail.conversation_index import find_parent_in_db
from src.mail.attachment_handler import AttachmentHandler
from src.mail.draft_handler import execute_draft_action
from src.notion.sync import NotionSync
from src.notify.feishu import FeishuNotifier
from src.models import Email, Attachment, TaskType, TaskPriority
from src.config import config
from src.scheduler.task_pool import global_task_pool


class WindowsWatcher:
    """Windows 版邮件同步工作者 & 核心事件循环"""

    def __init__(self, ai_trigger_queue=None, shutdown_event=None):
        self.arm = OutlookComArm()
        self.sync_store = SyncStore()
        self.notion_sync = NotionSync()
        self.attachment_handler = AttachmentHandler(config.notion_token)
        self.feishu = FeishuNotifier(
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            chat_id=config.feishu_chat_id,
            webhook_url=config.feishu_webhook_url
        )
        self.running = False
        self.ai_trigger_queue = ai_trigger_queue
        self.shutdown_event = shutdown_event

    async def start(self):
        logger.info("🚀 Starting MailAgent Windows Watcher (Central Task Loop)...")
        self.running = True
        
        # 1. 启动本地 Outlook COM 雷达，监控实时新收件和发件
        # start_radar 会自动把事件放入 global_task_pool (Priority 2)
        self.radar_thread = start_radar()
        
        # 2. 在后台异步补查历史邮件，放入 global_task_pool (Priority 3)
        self.catch_up_task = asyncio.create_task(self._catch_up(days=config.startup_lookback_days))
        
        self.background_tasks = set()
        self.mail_sync_semaphore = asyncio.Semaphore(3)  # 最多同时处理 3 个附件上传，防止爆内/过多并发
        
        # 3. 主循环：处理任务池中的事件
        while self.running:
            # 检查关停信号
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                logger.info("Shutdown event detected, stopping watcher...")
                self.running = False
                break

            try:
                task = global_task_pool.peek_task()
                if not task:
                    await asyncio.sleep(0.1)
                    continue

                if task.type == TaskType.MAIL_SYNC and len(self.background_tasks) >= 3:
                    # 由于我们主要是用 Semaphore 限制 MAIL_SYNC，但 asyncio.Semaphore 在任务 yield 之前
                    # 并没有真正 lock，这导致瞬间出队大量任务，破坏了优先级队列的设计。
                    # 这里直接通过限制 background_tasks 中 MAIL_SYNC 相关的任务数，
                    # 保证优先级队列能够真正发挥作用。
                    
                    # 统计正在执行的 MAIL_SYNC 任务数
                    sync_tasks = getattr(self, "_active_sync_tasks", 0)
                    if sync_tasks >= 3:
                        await asyncio.sleep(0.1)
                        continue
                    
                # 确认可以处理，正式出队
                task = global_task_pool.get_task_nowait()
                logger.info(f"📥 Dequeued task: {task.type.name} (Priority {task.priority_level})")
                
                if task.type == TaskType.MAIL_SYNC:
                    self._active_sync_tasks = getattr(self, "_active_sync_tasks", 0) + 1
                
                async def run_task(t=task):
                    try:
                        if t.type == TaskType.WEBHOOK_DRAFT:
                            # 高优先级：发送/保存草稿 (COM 操作在独立线程中执行，防阻塞)
                            await asyncio.to_thread(execute_draft_action, t.payload)
                            
                        elif t.type == TaskType.MAIL_SYNC:
                            # 中低优先级：邮件同步 (使用 Semaphore 限制并发)
                            entry_id = t.payload.get("entry_id")
                            store_id = t.payload.get("store_id")
                            trigger_ai = (t.priority_level <= TaskPriority.MEDIUM.value) or config.notion_ai_trigger_historical
                            async with self.mail_sync_semaphore:
                                await self.process_mail_sync(entry_id, store_id=store_id, trigger_ai=trigger_ai)
                                

                    except Exception as e:
                        logger.error(f"❌ Error executing task {t.type.name}: {e}")
                    finally:
                        if t.type == TaskType.MAIL_SYNC:
                            self._active_sync_tasks -= 1
                        global_task_pool.task_done()

                bg_task = asyncio.create_task(run_task())
                self.background_tasks.add(bg_task)
                bg_task.add_done_callback(self.background_tasks.discard)
                
            except Exception as e:
                logger.error(f"Error in main task loop: {e}")
                await asyncio.sleep(2)

    async def process_mail_sync(self, entry_id: str, store_id: Optional[str] = None, trigger_ai: bool = True):
        """处理同步任务：获取邮件 -> 转换内容 -> 写入 Notion -> (可选) 触发 AI"""
        if self.sync_store.is_synced(entry_id):
            return

        fetched = self.arm.fetch_by_entry_id(entry_id, store_id)
        if not fetched:
            logger.warning(f"Could not fetch email from Outlook: {entry_id[:16]}")
            return



        email = Email(
            message_id=fetched.message_id,
            subject=fetched.subject,
            sender=fetched.from_email,
            sender_name=fetched.from_name,
            to="; ".join(fetched.to),
            cc="; ".join(fetched.cc),
            date=datetime.fromisoformat(fetched.date_utc),
            content=fetched.html_body or fetched.text_body,
            content_type="text/html" if fetched.html_body else "text/plain",
            mailbox=fetched.mailbox,
            is_read=fetched.is_read,
            is_flagged=fetched.is_flagged,
            has_attachments=fetched.has_attachments,
            thread_id=fetched.conversation_id,
            in_reply_to=fetched.in_reply_to,
            internal_id=None,
        )

        if fetched.has_attachments:
            raw_item = self.arm.get_raw_item(entry_id)
            if raw_item:
                attachments = self.attachment_handler.extract(raw_item)
                for att in attachments:
                    email.attachments.append(Attachment(
                        filename=att.filename,
                        content_type=att.content_type,
                        size=att.size,
                        path=att.local_path,
                        content_id=att.content_id,
                        is_inline=att.is_inline
                    ))

        parent_page_url = find_parent_in_db(fetched.conversation_index, self.sync_store)
        
        try:
            page_id = await self.notion_sync.create_email_page_v2(email)
            if page_id:
                notion_url = f"https://notion.so/{page_id.replace('-', '')}"
                
                # 保存同步记录
                self.sync_store.save_sync_record(
                    entry_id=entry_id,
                    message_id=fetched.message_id,
                    conversation_id=fetched.conversation_id,
                    conversation_index=fetched.conversation_index,
                    notion_page_url=notion_url,
                    notion_page_id=page_id,
                    parent_page_url=parent_page_url or ""
                )
                
                if fetched.mailbox == "Inbox":
                    await self.feishu.notify_important_email({
                        "subject": email.subject,
                        "from_name": email.sender_name,
                        "from_email": email.sender,
                        "date": email.date.isoformat(),
                        "page_id": page_id,
                        "mailbox": fetched.mailbox,
                        "ai_priority": "🔵 一般", 
                        "ai_action": "查阅",
                        "ai_summary": email.content[:200] + "..."
                    })
                    
                logger.info(f"✅ Successfully uploaded to Notion: {email.subject}")
                
                self.arm.mark_as_read(entry_id)
                
                # 同步成功后通知 AI Controller
                if trigger_ai:
                    self._notify_ai_trigger()
                    
        except Exception as e:
            logger.error(f"Failed to sync email {email.subject}: {e}")

    def _notify_ai_trigger(self):
        """通知 AIWorker 有新邮件已同步，需要触发 AI 处理"""
        if self.ai_trigger_queue is None:
            return
        try:
            self.ai_trigger_queue.put_nowait({"type": "email_synced", "ts": time.time()})
        except Exception as e:
            logger.error(f"Failed to send AI trigger signal: {e}")

    async def _catch_up(self, days: int = 1):
        """启动时补查最近的邮件，赋予低优先级排队"""
        if days <= 0:
            logger.info("⏭️ Startup lookback disabled. Skipping catch-up.")
            return

        since = datetime.now() - timedelta(days=days)
        
        inbox_items = self.arm.iter_folder(OL_FOLDER_INBOX, since=since, return_dates=True)
        sent_items = self.arm.iter_folder(OL_FOLDER_SENT, since=since, return_dates=True)
        all_items = inbox_items + sent_items
        if not all_items:
            logger.info("✅ Catch-up complete: No emails found.")
            return

        # 补查的任务按照收发时间排队，并加入全局任务池 (Priority 3)
        new_count = 0
        for eid, sid, dt_val in all_items:
            if not self.sync_store.is_synced(eid):
                payload = {"entry_id": eid, "store_id": sid}
                ts = dt_val.timestamp() if isinstance(dt_val, datetime) else time.time()
                global_task_pool.add_task(TaskType.MAIL_SYNC, TaskPriority.LOW, payload, timestamp=ts)
                new_count += 1
        
        logger.info(f"✅ Scanned {len(all_items)} items. Queued {new_count} catch-up tasks (Priority 3 - Low).")

    async def stop(self):
        """主动停止监听并清理资源"""
        logger.info("Stopping MailAgent Windows Watcher...")
        self.running = False
        
        if hasattr(self, 'catch_up_task'):
            self.catch_up_task.cancel()
        
        await self.close()
        logger.info("MailAgent Windows Watcher stopped.")

    async def close(self):
        try:
            await self.notion_sync.close()
        except Exception:
            pass
        try:
            self.arm.close()
        except Exception:
            pass
