"""
Notion 日历库同步模块

将 CalendarEvent 对象同步（upsert）到 Notion Calendar Database。
支持：
  - 按 Event ID 去重 / 更新
  - 全字段映射（Title, Time, Status, Location, Attendees ...）
  - 取消事件处理
  - 邮件 <-> 日历 双向关联

Usage:
    sync = NotionCalendarSync()
    page_id = await sync.sync_event(event)
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from loguru import logger
import httpx
import httpcore

from src.models import CalendarEvent, EventStatus
from src.notion.client import NotionClient
from src.config import config
import logging
logger = logging.getLogger(__name__)
class NotionCalendarSync:
    """Notion 日历库同步器"""

    def __init__(self):
        self.client = NotionClient()
        self.calendar_db_id = config.calendar_database_id
        # 缓存 data_source_id
        self._ds_id: Optional[str] = None
        # 负面缓存：网络不可达时避免反复请求
        self._ds_id_fail_until: Optional[datetime] = None

    async def close(self):
        """关闭 HTTP 会话"""
        await self.client.close()

    async def _get_ds_id(self) -> str:
        """获取日历库的 data_source_id（带缓存 + 负面缓存）

        正面缓存：成功获取后永久缓存（直到进程重启）。
        负面缓存：网络异常时缓存 60 秒，避免每个事件都重复失败。
        """
        if self._ds_id:
            return self._ds_id

        # 检查负面缓存
        if self._ds_id_fail_until and datetime.now(timezone.utc) < self._ds_id_fail_until:
            raise ConnectionError("Notion API unreachable (cached failure, will retry later)")

        try:
            self._ds_id = await self.client.get_data_source_id(self.calendar_db_id)
            self._ds_id_fail_until = None  # 成功后清除负面缓存
            return self._ds_id
        except (httpx.ConnectError, httpcore.ConnectError, ConnectionError, OSError) as e:
            # 网络类异常：设置 60 秒负面缓存
            self._ds_id_fail_until = datetime.now(timezone.utc) + timedelta(seconds=60)
            logger.warning(f"Notion API unreachable, negative cache set for 60s: {type(e).__name__}")
            raise

    # ─── 核心公开方法 ───────────────────────────────────

    async def sync_event(
        self,
        event: CalendarEvent,
        email_page_id: Optional[str] = None,
        sequence: int = 0,
    ) -> Optional[str]:
        """同步单个日历事件到 Notion（upsert）

        Args:
            event: CalendarEvent 对象
            email_page_id: 关联的邮件页面 ID（用于 Email Inbox relation）
            sequence: iCal 序列号（用于判断是否需要更新）

        Returns:
            Notion page ID，失败返回 None
        """
        if not self.calendar_db_id:
            logger.debug("CALENDAR_DATABASE_ID not configured, skipping calendar sync")
            return None

        try:
            existing = await self._find_existing(event.event_id)

            if existing:
                page_id = existing["id"]
                # 检查是否需要更新
                existing_seq = self._get_existing_sequence(existing)
                if sequence > 0 and existing_seq >= sequence:
                    logger.debug(f"Calendar event '{event.title[:40]}' already up-to-date "
                                 f"(seq {existing_seq} >= {sequence})")
                    # 即使不更新属性，也可能需要补充 Email Inbox relation
                    if email_page_id:
                        await self._link_email(page_id, email_page_id, existing)
                    return page_id

                # 更新已有页面
                logger.debug(f"Updating calendar event: '{event.title[:50]}' (page_id={page_id})")
                properties = self._build_properties(event, email_page_id, sequence, existing)
                await self.client.client.pages.update(page_id=page_id, properties=properties)
                logger.debug(f"Updated calendar event: '{event.title[:50]}'")
                return page_id
            else:
                # 创建新页面
                properties = self._build_properties(event, email_page_id, sequence)
                ds_id = await self._get_ds_id()

                icon = {"type": "emoji", "emoji": "📅"}
                page = await self.client.client.pages.create(
                    parent={"data_source_id": ds_id},
                    properties=properties,
                    icon=icon,
                )
                page_id = page["id"]
                logger.info(f"📅 Created calendar event: '{event.title[:50]}'")
                return page_id

        except Exception as e:
            logger.error(f"Failed to sync calendar event '{event.title[:50]}': {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    async def sync_events(self, events: List[CalendarEvent]) -> int:
        """批量同步日历事件

        Args:
            events: CalendarEvent 列表

        Returns:
            成功同步的数量
        """
        if not events:
            return 0

        success_count = 0
        for event in events:
            page_id = await self.sync_event(event)
            if page_id:
                success_count += 1

        logger.info(f"📅 Calendar sync complete: {success_count}/{len(events)} events synced")
        return success_count

    # ─── 查询方法 ─────────────────────────────────────

    async def _find_existing(self, event_id: str) -> Optional[Dict[str, Any]]:
        """按 Event ID 查找已有页面

        Args:
            event_id: 日历事件唯一标识

        Returns:
            Notion page dict 或 None
        """
        if not event_id:
            return None

        try:
            ds_id = await self._get_ds_id()
            results = await self.client.client.data_sources.query(
                data_source_id=ds_id,
                filter={
                    "property": "Event ID",
                    "rich_text": {"equals": event_id},
                },
                page_size=1,
            )
            pages = results.get("results", [])
            return pages[0] if pages else None

        except Exception as e:
            logger.warning(f"Failed to query calendar event by Event ID '{event_id[:40]}': {e}")
            return None

    async def query_all_event_ids(self) -> set:
        """查询日历库中所有 Event ID，用于批量去重"""
        event_ids = set()
        try:
            ds_id = await self._get_ds_id()
            has_more, cursor = True, None
            while has_more:
                query_params = {
                    "data_source_id": ds_id,
                    "filter": {"property": "Event ID", "rich_text": {"is_not_empty": True}},
                    "page_size": 100,
                }
                if cursor:
                    query_params["start_cursor"] = cursor
                results = await self.client.client.data_sources.query(**query_params)
                for page in results.get("results", []):
                    rt = page.get("properties", {}).get("Event ID", {}).get("rich_text", [])
                    if rt and rt[0].get("text", {}).get("content"):
                        event_ids.add(rt[0]["text"]["content"])
                has_more = results.get("has_more", False)
                cursor = results.get("next_cursor")
            return event_ids
        except Exception as e:
            logger.error(f"Failed to query all calendar event IDs: {e}")
            return event_ids

    # ─── 属性构建 ─────────────────────────────────────

    def _build_properties(
        self,
        event: CalendarEvent,
        email_page_id: Optional[str] = None,
        sequence: int = 0,
        existing: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """构建 Notion page properties

        严格对齐用户日历库的 27 个属性。
        """
        # 处理时间（确保有时区信息）
        start = event.start_time
        end = event.end_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=config.tz)
        if end and end.tzinfo is None:
            end = end.replace(tzinfo=config.tz)

        # 时间属性
        time_prop: Dict[str, Any] = {"start": start.isoformat()}
        if end and not event.is_all_day:
            time_prop["end"] = end.isoformat()
        elif event.is_all_day and end:
            # 全天事件：Notion 需要纯日期格式
            time_prop = {
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
            }

        # Status 映射
        status_map = {
            EventStatus.CONFIRMED: "confirmed",
            EventStatus.TENTATIVE: "tentative",
            EventStatus.CANCELLED: "cancelled",
            EventStatus.NONE: "tentative",
        }
        status_name = status_map.get(event.status, "tentative")

        # 会议类型推断
        meeting_type = self._infer_meeting_type(event)

        now_iso = datetime.now(config.tz).isoformat()

        properties: Dict[str, Any] = {
            # 1. Title (title)
            "Title": {"title": [{"text": {"content": event.title[:2000]}}]},
            # 7. Time (date)
            "Time": {"date": time_prop},
            # 4. Status (select)
            "Status": {"select": {"name": status_name}},
            # 6. Calendar (select)
            "Calendar": {"select": {"name": event.calendar_name or "Outlook"}},
            "Last Modified": {"date": {"start": (event.last_modified or datetime.now(config.tz)).isoformat()}},
            # 9. Last Synced (date)
            "Last Synced": {"date": {"start": now_iso}},
            # 10. Is All Day (checkbox)
            "Is All Day": {"checkbox": event.is_all_day},
            # 11. Is Recurring (checkbox)
            "Is Recurring": {"checkbox": event.is_recurring},
            # 18. Event ID (text)
            "Event ID": {"rich_text": [{"text": {"content": event.event_id[:2000]}}]},
            # 21. Sequence (number)
            "Sequence": {"number": sequence},
            # 5. Sync Status (select)
            "Sync Status": {"select": {"name": "Synced"}},
        }

        # 3. 会议类型 (select)
        if meeting_type:
            properties["会议类型"] = {"select": {"name": meeting_type}}

        # 12. Description (text) — 截断避免过长
        if event.description:
            desc = event.description[:2000]
            properties["Description"] = {"rich_text": [{"text": {"content": desc}}]}

        # 13. Attendees (text)
        if event.attendees:
            att_str = event.attendees_str[:2000]
            properties["Attendees"] = {"rich_text": [{"text": {"content": att_str}}]}

        # 14. Attendee Count (number)
        properties["Attendee Count"] = {"number": event.attendee_count}

        # 15. Location (text)
        if event.location:
            properties["Location"] = {"rich_text": [{"text": {"content": event.location[:2000]}}]}

        # 16. Organizer (text)
        if event.organizer:
            properties["Organizer"] = {"rich_text": [{"text": {"content": event.organizer[:2000]}}]}

        # 17. Organizer Email (email)
        if event.organizer_email:
            properties["Organizer Email"] = {"email": event.organizer_email[:100]}

        # 19. URL (url)
        if event.url:
            properties["URL"] = {"url": event.url}

        # 20. Recurrence Rule (text)
        if event.recurrence_rule:
            properties["Recurrence Rule"] = {"rich_text": [{"text": {"content": event.recurrence_rule[:2000]}}]}

        # 22. Email Inbox (relation)
        if email_page_id:
            # 保留已有的 relation 并追加
            existing_relations = []
            if existing:
                existing_rel = existing.get("properties", {}).get("Email Inbox", {}).get("relation", [])
                existing_relations = [{"id": r["id"]} for r in existing_rel]

            # 避免重复
            if not any(r["id"] == email_page_id for r in existing_relations):
                existing_relations.append({"id": email_page_id})

            properties["Email Inbox"] = {"relation": existing_relations}

        return properties

    # ─── 辅助方法 ─────────────────────────────────────

    async def _link_email(self, page_id: str, email_page_id: str, existing: Dict):
        """将邮件页面关联到日历事件（仅追加 relation，不覆盖）"""
        try:
            existing_rel = existing.get("properties", {}).get("Email Inbox", {}).get("relation", [])
            relations = [{"id": r["id"]} for r in existing_rel]
            if any(r["id"] == email_page_id for r in relations):
                return  # 已关联
            relations.append({"id": email_page_id})
            await self.client.client.pages.update(
                page_id=page_id,
                properties={"Email Inbox": {"relation": relations}},
            )
            logger.debug(f"Linked email page {email_page_id} to calendar event {page_id}")
        except Exception as e:
            logger.warning(f"Failed to link email to calendar event: {e}")

    @staticmethod
    def _get_existing_sequence(page: Dict) -> int:
        """从已有 Notion 页面中提取 Sequence 值"""
        try:
            return page.get("properties", {}).get("Sequence", {}).get("number", 0) or 0
        except Exception:
            return 0

    @staticmethod
    def _infer_meeting_type(event: CalendarEvent) -> Optional[str]:
        """根据事件内容推断会议类型"""
        title_lower = (event.title or "").lower()
        desc_lower = (event.description or "").lower()
        url = event.url or ""

        if "teams.microsoft.com" in url or "teams" in desc_lower:
            return "Teams 会议"
        if "zoom.us" in url or "zoom" in desc_lower:
            return "Zoom 会议"
        if "meet.google.com" in url:
            return "Google Meet"
        if "webex" in url.lower() or "webex" in desc_lower:
            return "Webex 会议"
        if event.location:
            return "线下会议"
        if event.attendees:
            return "在线会议"
        return None
