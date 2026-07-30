"""
Outlook Calendar Reader (Windows COM)

通过 Outlook COM API 读取日历文件夹中的事件，
转换为 CalendarEvent 对象供 Notion 同步使用。

Usage:
    reader = OutlookCalendarReader()
    events = reader.read_events(past_days=7, future_days=90)
"""

import re
import time
import pythoncom
import win32com.client
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from loguru import logger

from src.models import CalendarEvent, EventStatus, Attendee
from src.config import config


BEIJING_TZ = timezone(timedelta(hours=8))

# Outlook 常量
OL_APPOINTMENT = 26        # olAppointment
OL_MEETING_REQUEST = 53    # olMeetingRequest
OL_FOLDER_CALENDAR = 9     # olFolderCalendar

# 会议状态映射
OL_MEETING_STATUS = {
    0: EventStatus.NONE,         # olNonMeeting
    1: EventStatus.TENTATIVE,    # olMeeting (组织者)
    3: EventStatus.TENTATIVE,    # olMeetingReceived (待答复)
    5: EventStatus.CANCELLED,    # olMeetingCanceled
    7: EventStatus.TENTATIVE,    # olMeetingReceivedAndCanceled
}

# 响应状态映射
OL_RESPONSE_STATUS = {
    0: "unknown",      # olResponseNone
    1: "unknown",      # olResponseOrganized
    2: "tentative",    # olResponseTentative
    3: "accepted",     # olResponseAccepted
    4: "declined",     # olResponseDeclined
    5: "pending",      # olResponseNotResponded
}

# Teams URL 模式
TEAMS_URL_PATTERNS = [
    r'https://teams\.microsoft\.com/l/meetup-join/[^\s<>"\'\\]+',
    r'https://teams\.microsoft\.com/meet/\d+\?p=[A-Za-z0-9]+',
]


class OutlookCalendarReader:
    """通过 COM API 读取 Outlook 日历事件"""

    def __init__(self):
        self._initialized = False

    def _ensure_com_init(self):
        """确保 COM 已初始化（线程安全）"""
        if not self._initialized:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass  # 已在当前线程初始化过
            self._initialized = True

    def read_events(
        self,
        past_days: int = 7,
        future_days: int = 90,
    ) -> List[CalendarEvent]:
        """读取指定范围内的日历事件

        Args:
            past_days: 向过去回溯的天数
            future_days: 向未来展望的天数

        Returns:
            CalendarEvent 列表
        """
        self._ensure_com_init()

        try:
            app = win32com.client.Dispatch("Outlook.Application")
            ns = app.GetNamespace("MAPI")
        except Exception as e:
            logger.error(f"Failed to connect Outlook for calendar read: {e}")
            return []

        # 查找日历文件夹
        cal_folder = self._find_calendar_folder(ns)
        if not cal_folder:
            logger.error("Failed to find Outlook calendar folder")
            return []

        # 构造日期过滤范围
        now = datetime.now()
        start_date = now - timedelta(days=past_days)
        end_date = now + timedelta(days=future_days)

        start_str = start_date.strftime("%m/%d/%Y %H:%M %p")
        end_str = end_date.strftime("%m/%d/%Y %H:%M %p")

        events: List[CalendarEvent] = []

        try:
            items = cal_folder.Items
            items.Sort("[Start]")
            items.IncludeRecurrences = True

            # Restrict 过滤条件
            restriction = (
                f"[Start] >= '{start_str}' AND [End] <= '{end_str}'"
            )
            restricted_items = items.Restrict(restriction)

            count = 0
            for item in restricted_items:
                try:
                    event = self._convert_item(item)
                    if event:
                        events.append(event)
                        count += 1
                except Exception as e:
                    subject = getattr(item, "Subject", "<unknown>")
                    logger.debug(f"Failed to convert calendar item '{subject}': {e}")

            logger.info(f"📅 Read {count} calendar events from Outlook "
                        f"({start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')})")

        except Exception as e:
            logger.error(f"Failed to read calendar items: {e}")

        return events

    def _find_calendar_folder(self, ns):
        """查找指定账户的日历文件夹"""
        target_account = config.mail_account_name

        try:
            stores = ns.Stores
            for i in range(1, stores.Count + 1):
                store = stores.Item(i)
                if target_account.lower() in store.DisplayName.lower():
                    root = store.GetRootFolder()
                    folders = root.Folders
                    for j in range(1, folders.Count + 1):
                        folder = folders.Item(j)
                        # DefaultItemType = 1 表示 olAppointmentItem（日历）
                        if folder.DefaultItemType == 1:
                            logger.info(f"📍 Found calendar folder: '{folder.Name}' "
                                        f"in store '{store.DisplayName}'")
                            return folder
        except Exception as e:
            logger.warning(f"Error searching for calendar folder in account: {e}")

        # Fallback: 默认日历
        try:
            folder = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
            logger.info(f"📍 Using default calendar folder: '{folder.Name}'")
            return folder
        except Exception as e:
            logger.error(f"Failed to get default calendar folder: {e}")
            return None

    def _convert_item(self, item) -> Optional[CalendarEvent]:
        """将 Outlook COM AppointmentItem 转换为 CalendarEvent"""
        # 跳过非日历条目
        item_class = getattr(item, "Class", 0)
        if item_class != OL_APPOINTMENT:
            return None

        subject = getattr(item, "Subject", "") or "(无标题)"
        entry_id = getattr(item, "EntryID", "") or ""

        # 获取全局唯一ID (GlobalAppointmentID 是 bytes 对象)
        global_id = ""
        try:
            raw_id = getattr(item, "GlobalAppointmentID", "")
            if raw_id:
                if isinstance(raw_id, (bytes, bytearray)):
                    global_id = raw_id.hex()
                else:
                    global_id = str(raw_id)
        except Exception:
            pass

        # 优先用 GlobalAppointmentID，fallback 到 EntryID
        event_id = global_id or entry_id
        if not event_id:
            logger.debug(f"Calendar item '{subject[:40]}' has no ID, skipping")
            return None

        # 解析时间
        start_time = self._parse_com_datetime(getattr(item, "Start", None))
        end_time = self._parse_com_datetime(getattr(item, "End", None))

        if not start_time:
            logger.debug(f"Calendar item '{subject[:40]}' has no start time, skipping")
            return None

        if not end_time:
            end_time = start_time + timedelta(hours=1)

        is_all_day = bool(getattr(item, "AllDayEvent", False))

        # 地点
        location = getattr(item, "Location", "") or None

        # 描述（正文）
        description = None
        try:
            description = getattr(item, "Body", "") or None
        except Exception:
            pass

        # URL：优先尝试提取 Teams 链接
        url = None
        if description:
            url = self._extract_meeting_url(description)

        # 状态
        meeting_status = getattr(item, "MeetingStatus", 0)
        status = OL_MEETING_STATUS.get(meeting_status, EventStatus.NONE)

        # 如果事件已被用户接受/拒绝，覆盖状态
        response_status = getattr(item, "ResponseStatus", 0)
        if response_status == 3:  # Accepted
            status = EventStatus.CONFIRMED
        elif response_status == 4:  # Declined
            status = EventStatus.CANCELLED

        # 组织者
        organizer = getattr(item, "Organizer", "") or None
        organizer_email = None
        try:
            # 尝试从 PropertyAccessor 获取组织者邮箱
            pa = item.PropertyAccessor
            organizer_email = pa.GetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x0042001F"
            ) or None
        except Exception:
            pass

        # 参与者
        attendees = self._parse_recipients(item)

        # 重复规则
        is_recurring = bool(getattr(item, "IsRecurring", False))
        recurrence_rule = None
        if is_recurring:
            try:
                pattern = item.GetRecurrencePattern()
                recurrence_rule = f"Type={pattern.RecurrenceType}, Interval={pattern.Interval}"
            except Exception:
                pass

        # 最后修改时间
        last_modified = self._parse_com_datetime(
            getattr(item, "LastModificationTime", None)
        )

        return CalendarEvent(
            event_id=event_id,
            calendar_name="Outlook",
            title=subject,
            start_time=start_time,
            end_time=end_time,
            is_all_day=is_all_day,
            location=location,
            description=description,
            url=url,
            status=status,
            organizer=organizer,
            organizer_email=organizer_email,
            attendees=attendees,
            is_recurring=is_recurring,
            recurrence_rule=recurrence_rule,
            last_modified=last_modified,
        )

    def _parse_recipients(self, item) -> List[Attendee]:
        """解析参与者列表"""
        attendees = []
        try:
            recipients = item.Recipients
            if not recipients:
                return attendees

            for i in range(1, min(recipients.Count + 1, 51)):  # 最多 50 个
                try:
                    recip = recipients.Item(i)
                    name = recip.Name or ""
                    email = ""

                    # 尝试获取 SMTP 地址
                    try:
                        if recip.AddressEntry:
                            ae = recip.AddressEntry
                            if ae.Type == "EX":
                                exchange_user = ae.GetExchangeUser()
                                if exchange_user:
                                    email = exchange_user.PrimarySmtpAddress or ""
                            else:
                                email = ae.Address or ""
                    except Exception:
                        email = recip.Address or ""

                    # 解析响应状态
                    resp = getattr(recip, "MeetingResponseStatus", 0)
                    status = OL_RESPONSE_STATUS.get(resp, "unknown")

                    attendees.append(Attendee(
                        email=email,
                        name=name if name else None,
                        status=status,
                    ))
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Failed to parse recipients: {e}")

        return attendees

    @staticmethod
    def _parse_com_datetime(dt) -> Optional[datetime]:
        """将 COM datetime (pywintypes.datetime) 转为 Python datetime"""
        if dt is None:
            return None
        try:
            # pywintypes.datetime 可能带有时区信息
            py_dt = datetime(
                dt.year, dt.month, dt.day,
                dt.hour, dt.minute, dt.second,
                tzinfo=BEIJING_TZ,  # Outlook COM 返回的是本地时间
            )
            return py_dt
        except Exception:
            return None

    @staticmethod
    def _extract_meeting_url(text: str) -> Optional[str]:
        """从文本中提取在线会议链接"""
        if not text:
            return None

        # Teams
        for pattern in TEAMS_URL_PATTERNS:
            match = re.search(pattern, text)
            if match:
                return match.group(0).rstrip('>')

        # Zoom
        zoom_match = re.search(r'https://[\w.-]*zoom\.us/j/\d+[^\s<>"\']*', text)
        if zoom_match:
            return zoom_match.group(0)

        # Google Meet
        meet_match = re.search(r'https://meet\.google\.com/[a-z]+-[a-z]+-[a-z]+', text)
        if meet_match:
            return meet_match.group(0)

        # Webex
        webex_match = re.search(r'https://[\w.-]*webex\.com/\S+', text)
        if webex_match:
            return webex_match.group(0)

        return None
