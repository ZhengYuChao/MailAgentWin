"""
Outlook COM Arm: 替代 macOS 的 AppleScript Arm。
输入: EntryID
输出: 与 Mac 版 reader.MailMessage 兼容的 dict
"""
from __future__ import annotations

import logging
import pythoncom
import win32com.client
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

# ===== MAPI DASL 属性常量 =====
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_TRANSPORT_MESSAGE_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
PR_IN_REPLY_TO = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"

OL_MAIL_CLASS = 43          # olMail
OL_MEETING_REQUEST_CLASS = 53    # olMeetingRequest
OL_MEETING_RESPONSE_NEG_CLASS = 54
OL_MEETING_RESPONSE_POS_CLASS = 55
OL_MEETING_RESPONSE_TEN_CLASS = 56
OL_MEETING_CANCELLATION_CLASS = 57
OL_FOLDER_INBOX = 6         # olFolderInbox
OL_FOLDER_SENT = 5          # olFolderSentMail

@dataclass
class FetchedMail:
    entry_id: str
    message_id: str             # RFC 822
    conversation_id: str
    conversation_index: str     # hex
    in_reply_to: Optional[str]
    subject: str
    from_email: str
    from_name: str
    to: list[str]
    cc: list[str]
    date_utc: str               # ISO-8601
    html_body: str
    text_body: str
    is_read: bool
    is_flagged: bool
    mailbox: str                # 收件箱 / 发件箱
    has_attachments: bool
    raw_headers: str

class OutlookComArm:
    def __init__(self):
        # 必须在使用 COM 的线程里调用一次
        pythoncom.CoInitialize()
        self.app = win32com.client.Dispatch("Outlook.Application")
        self.ns = self.app.GetNamespace("MAPI")

    def close(self):
        pythoncom.CoUninitialize()

    def _reconnect(self):
        try:
            import pythoncom
            import time
            logger.warning("Attempting to reconnect to Outlook COM...")
            # 释放可能存在的旧引用
            self.app = None
            self.ns = None
            time.sleep(1.0) # 给 Outlook 1秒钟喘息时间
            
            pythoncom.CoInitialize()
            self.app = win32com.client.Dispatch("Outlook.Application")
            self.ns = self.app.GetNamespace("MAPI")
            logger.info("✅ Successfully reconnected to Outlook COM server.")
        except Exception as e:
            logger.error(f"❌ Failed to reconnect to Outlook: {e}")

    def _get_folder_by_account(self, folder_kind: int):
        """尝试在指定账户中查找文件夹，如果找不到则返回默认文件夹"""
        from src.config import config
        target_account = config.mail_account_name
        
        logger.info(f"Searching for account containing: '{target_account}'...")
        
        try:
            stores = self.ns.Stores
            for i in range(1, stores.Count + 1):
                store = stores.Item(i)
                store_name = store.DisplayName
                
                if target_account.lower() in store_name.lower():
                    logger.info(f"📍 Found matching store: '{store_name}'")
                    root = store.GetRootFolder()
                    sid = store.StoreID # 获取 StoreID
                    
                    folders = root.Folders
                    found_folder = None
                    for j in range(1, folders.Count + 1):
                        folder = folders.Item(j)
                        fname = folder.Name
                        if folder.DefaultItemType == 0: # olMailItem
                            if folder_kind == OL_FOLDER_INBOX and fname.lower() in ["收件箱", "inbox"]:
                                found_folder = folder
                            if folder_kind == OL_FOLDER_SENT and fname.lower() in ["已发送邮件", "sent items", "sent", "已发送"]:
                                found_folder = folder
                    
                    if found_folder:
                        # 给文件夹对象动态挂载一个 store_id 属性，方便后面读取
                        # 在 COM 对象上直接挂载可能不行，我们后面通过 folder.Parent 获取
                        return found_folder
            
            return self.ns.GetDefaultFolder(folder_kind)
        except Exception as e:
            logger.error(f"Error finding folder in account: {e}")
            return self.ns.GetDefaultFolder(folder_kind)

    def get_inbox_unread_count(self) -> int:
        """获取收件箱中的未读邮件数量"""
        try:
            folder = self._get_folder_by_account(OL_FOLDER_INBOX)
            return folder.UnReadItemCount
        except Exception as e:
            logger.error(f"Failed to get inbox unread count: {e}")
            return 0

    def get_raw_item(self, entry_id: str, store_id: Optional[str] = None):
        """获取原始 COM 对象，用于附件提取等操作"""
        try:
            return self.ns.GetItemFromID(entry_id, store_id) if store_id else self.ns.GetItemFromID(entry_id)
        except Exception as e:
            err_str = str(e)
            if any(code in err_str for code in ["-2147220995", "-2147023179"]) or "not connected" in err_str.lower():
                logger.warning(f"COM interface invalid ({err_str}), reconnecting...")
                self._reconnect()
                try:
                    return self.ns.GetItemFromID(entry_id, store_id) if store_id else self.ns.GetItemFromID(entry_id)
                except Exception as e2:
                    logger.error(f"GetItemFromID failed again after reconnect: {e2}")
                    return None
            else:
                logger.warning(f"GetItemFromID failed: {e}")
                return None

    def mark_as_read(self, entry_id: str, store_id: Optional[str] = None):
        """将邮件标记为已读"""
        item = self.get_raw_item(entry_id, store_id)
        if item:
            try:
                if getattr(item, "UnRead", False):
                    item.UnRead = False
                    item.Save()
                    logger.info(f"Marked email as read: {entry_id[:16]}")
            except Exception as e:
                logger.error(f"Failed to mark email as read: {e}")

    # ---------- 核心：按 EntryID 取邮件 ----------
    def fetch_by_entry_id(self, entry_id: str, store_id: Optional[str] = None) -> Optional[FetchedMail]:
        """O(1) 取邮件，不要遍历文件夹。"""
        try:
            # 这里的 store_id 非常重要，能极大提升在多账户下的定位速度和稳定性
            item = self.ns.GetItemFromID(entry_id, store_id) if store_id else self.ns.GetItemFromID(entry_id)
        except Exception as e:
            err_str = str(e)
            if any(code in err_str for code in ["-2147220995", "-2147023179"]) or "not connected" in err_str.lower():
                logger.warning(f"COM interface invalid ({err_str}), reconnecting...")
                self._reconnect()
                try:
                    item = self.ns.GetItemFromID(entry_id, store_id) if store_id else self.ns.GetItemFromID(entry_id)
                except Exception as e2:
                    logger.error(f"GetItemFromID failed again after reconnect: {e2}")
                    return None
            else:
                logger.warning(f"GetItemFromID failed: {e}")
                return None

        if item.Class not in (OL_MAIL_CLASS, OL_MEETING_REQUEST_CLASS, 
                             OL_MEETING_RESPONSE_NEG_CLASS, OL_MEETING_RESPONSE_POS_CLASS, 
                             OL_MEETING_RESPONSE_TEN_CLASS, OL_MEETING_CANCELLATION_CLASS):
            return None

        pa = item.PropertyAccessor
        message_id = self._safe_get(pa, PR_INTERNET_MESSAGE_ID, "")
        in_reply_to = self._safe_get(pa, PR_IN_REPLY_TO, "") or None
        raw_headers = self._safe_get(pa, PR_TRANSPORT_MESSAGE_HEADERS, "")

        # 尝试获取 SMTP 地址 (处理 Exchange 地址太长的问题)
        from_email = getattr(item, "SenderEmailAddress", "") or ""
        if from_email and not "@" in from_email:
            try:
                # 尝试从 Sender 对象获取 SMTP 地址
                if item.SenderEmailType == "EX":
                    sender = item.Sender
                    if sender:
                        from_email = sender.GetExchangeUser().PrimarySmtpAddress or from_email
            except Exception:
                pass

        return FetchedMail(
            entry_id=item.EntryID,
            message_id=message_id,
            conversation_id=getattr(item, "ConversationID", "") or "",
            conversation_index=getattr(item, "ConversationIndex", "") or "",
            in_reply_to=in_reply_to,
            subject=getattr(item, "Subject", "") or "",
            from_email=self._truncate_email(from_email),
            from_name=getattr(item, "SenderName", "") or "",
            to=self._split_recipients(getattr(item, "To", "")),
            cc=self._split_recipients(getattr(item, "CC", "")),
            date_utc=self._to_iso_utc(getattr(item, "ReceivedTime", None)),
            html_body=getattr(item, "HTMLBody", "") or "",
            text_body=getattr(item, "Body", "") or "",
            is_read=not bool(getattr(item, "UnRead", False)),
            is_flagged=int(getattr(item, "FlagStatus", 0)) == 2,  # 2 = olFlagMarked
            mailbox=self._infer_mailbox(item),
            has_attachments=getattr(item.Attachments, "Count", 0) > 0 if hasattr(item, "Attachments") else False,
            raw_headers=raw_headers,
        )

    def _get_folder_by_account_with_ns(self, ns, folder_kind: int):
        from src.config import config
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
                        fname = folder.Name
                        if folder.DefaultItemType == 0:
                            if folder_kind == OL_FOLDER_INBOX and fname.lower() in ["收件箱", "inbox"]:
                                return folder
                            if folder_kind == OL_FOLDER_SENT and fname.lower() in ["已发送邮件", "sent items", "sent", "已发送"]:
                                return folder
            return ns.GetDefaultFolder(folder_kind)
        except Exception:
            return ns.GetDefaultFolder(folder_kind)

    def iter_folder(self, folder_kind: int = OL_FOLDER_INBOX, since: Optional[datetime] = None,
                    limit: Optional[int] = None, return_dates: bool = False):
        """返回 (EntryID, StoreID) 或 (EntryID, StoreID, date) 列表。支持多线程调用。"""
        import pythoncom
        pythoncom.CoInitialize()
        
        # 强制在当前线程创建一个局部 Namespace
        try:
            local_app = win32com.client.Dispatch("Outlook.Application")
            local_ns = local_app.GetNamespace("MAPI")
        except Exception as e:
            logger.error(f"Failed to connect Outlook in scan thread: {e}")
            return []

        try:
            folder = self._get_folder_by_account_with_ns(local_ns, folder_kind)
            try:
                store_id = folder.Store.StoreID
            except Exception:
                store_id = None
            logger.info(f"📂 Scanning folder: '{folder.Name}'...")
        except Exception as e:
            logger.error(f"Failed to access folder for scan: {e}")
            return []
                
        date_prop = "ReceivedTime" if folder_kind == OL_FOLDER_INBOX else "SentOn"
        prop_tag = "0x0E060040" if folder_kind == OL_FOLDER_INBOX else "0x00390040"
        
        restrict = ""
        if since:
            iso_since = since.strftime('%Y-%m-%d %H:%M')
            restrict = f"@SQL=\"http://schemas.microsoft.com/mapi/proptag/{prop_tag}\" >= '{iso_since}'"
        
        results = []
        try:
            table = folder.GetTable(restrict)
            table.Columns.RemoveAll()
            table.Columns.Add("EntryID")
            table.Columns.Add(date_prop)
            try:
                table.Columns.Add("http://schemas.microsoft.com/mapi/proptag/0x0FF40102")
            except Exception: pass
                
            table.Sort(date_prop, True)
            
            while not table.EndOfTable:
                row = table.GetNextRow()
                eid = row.Item("EntryID")
                dt = row.Item(date_prop)
                
                # 转换 pywintypes.datetime 为 Python datetime
                if dt:
                    py_dt = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=timezone.utc)
                else:
                    py_dt = datetime.min.replace(tzinfo=timezone.utc)
                
                # 兼容性列检查
                has_sid_col = False
                for c_idx in range(1, table.Columns.Count + 1):
                    if table.Columns.Item(c_idx).Name == "http://schemas.microsoft.com/mapi/proptag/0x0FF40102":
                        has_sid_col = True
                        break
                
                sid = row.Item("http://schemas.microsoft.com/mapi/proptag/0x0FF40102") if has_sid_col else store_id
                
                if return_dates:
                    results.append((eid, sid, py_dt))
                else:
                    results.append((eid, sid))
                if limit and len(results) >= limit: break
            
            if results:
                logger.info(f"✅ Fast scan complete: found {len(results)} items")
                return results
        except Exception as e:
            logger.warning(f"Fast scan failed ({e}), falling back to legacy...")
 
        # Legacy 模式
        legacy_restrict = f"[{date_prop}] >= '{since.strftime('%Y-%m-%d %H:%M')}'" if since else ""
        items = folder.Items.Restrict(legacy_restrict) if legacy_restrict else folder.Items
        items.Sort(f"[{date_prop}]", True)
        
        count = items.Count
        if count > 0:
            step = max(1, count // 10)
            for i in range(1, count + 1):
                try:
                    item = items.Item(i)
                    dt = getattr(item, date_prop, None)
                    if dt:
                        py_dt = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=timezone.utc)
                    else:
                        py_dt = datetime.min.replace(tzinfo=timezone.utc)
                        
                    if return_dates:
                        results.append((item.EntryID, store_id, py_dt))
                    else:
                        results.append((item.EntryID, store_id))
                    if i % step == 0:
                        logger.info(f"  ... collecting IDs: {i}/{count}")
                    if limit and len(results) >= limit: break
                except Exception: continue
        
        return results

    # ---------- 工具函数 ----------
    @staticmethod
    def _safe_get(pa, prop_tag: str, default=""):
        try:
            return pa.GetProperty(prop_tag) or default
        except Exception:
            return default

    @staticmethod
    def _split_recipients(s: str) -> list[str]:
        if not s:
            return []
        return [x.strip() for x in s.replace(";", ",").split(",") if x.strip()]

    @staticmethod
    def _to_iso_utc(dt) -> str:
        if dt is None:
            return ""
        # dt 是 pywintypes.datetime，可以直接转换为 python datetime
        py_dt = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=timezone.utc)
        return py_dt.isoformat()

    @staticmethod
    def _infer_mailbox(item) -> str:
        from src.config import config
        try:
            parent_name = item.Parent.Name
        except Exception:
            return "Inbox"
            
        configured_sent_name = config.mail_sent_name.lower()
        if (configured_sent_name and configured_sent_name in parent_name.lower() or
            "sent" in parent_name.lower() or 
            "已发送" in parent_name or 
            "发件" in parent_name):
            return "Sent"
        return "Inbox"

    @staticmethod
    def _truncate_email(email: str) -> str:
        """Notion email 字段限制 100 字符"""
        if not email: return ""
        # 如果是 "Name <email>" 格式，提取 email
        if "<" in email and ">" in email:
            import re
            match = re.search(r'<(.*?)>', email)
            if match:
                email = match.group(1)
        return email[:100]
