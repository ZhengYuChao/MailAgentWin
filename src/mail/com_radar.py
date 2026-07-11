"""
订阅 Outlook NewMailEx 事件 + 兜底轮询，统一吐 EntryID 到全局任务池。
COM 事件回调里只做最轻量动作，绝对禁止发 HTTP / 调 Notion。
"""
import threading, time, logging
import pythoncom, win32com.client
from src.scheduler.task_pool import global_task_pool
from src.models import TaskType, TaskPriority

log = logging.getLogger(__name__)

class OutlookEventSink:
    def __init__(self):
        # 注意：DispatchWithEvents 会自动寻找 OnNewMailEx 等方法
        pass

    def OnNewMailEx(self, entry_ids: str):
        # entry_ids 是逗号分隔的多个 EntryID
        for eid in entry_ids.split(","):
            eid = eid.strip()
            if eid:
                payload = {"entry_id": eid, "store_id": None, "mailbox_type": "new"}
                global_task_pool.add_task(TaskType.MAIL_SYNC, TaskPriority.MEDIUM, payload)
                log.info(f"NewMailEx queued (Priority 2 - Medium): {eid[:24]}")

class SentFolderEventSink:
    def __init__(self):
        pass

    def OnItemAdd(self, item):
        try:
            eid = getattr(item, 'EntryID', None)
            if eid:
                payload = {"entry_id": eid, "store_id": None, "mailbox_type": "sent"}
                global_task_pool.add_task(TaskType.MAIL_SYNC, TaskPriority.MEDIUM, payload)
                log.info(f"Sent folder ItemAdd queued (Priority 2 - Medium): {eid[:24]}")
        except Exception as e:
            log.error(f"Error in Sent folder ItemAdd: {e}")

def start_radar(poll_interval: int = 60) -> threading.Thread:
    """启动事件订阅 + 兜底轮询线程。"""
    def _run():
        pythoncom.CoInitialize()
        try:
            app = win32com.client.DispatchWithEvents("Outlook.Application", OutlookEventSink)
            
            # 绑定已发送文件夹的 ItemAdd 事件
            ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            sent_folder = ns.GetDefaultFolder(5) # olFolderSentMail
            sent_items = sent_folder.Items
            sent_events = win32com.client.WithEvents(sent_items, SentFolderEventSink)
            
            # 保持引用，防止被垃圾回收导致事件失效
            app._sent_items_ref = sent_items
            app._sent_events_ref = sent_events
            
            log.info("Outlook COM Radar started (Event listening enabled for Inbox & Sent Items)")
            
            last_poll = 0
            while True:
                # 必须持续泵送消息以触发事件回调
                pythoncom.PumpWaitingMessages()
                
                now = time.time()
                if now - last_poll > poll_interval:
                    # 兜底逻辑可以在这里实现（例如扫描最近文件夹）
                    last_poll = now
                
                time.sleep(0.5)
        except Exception as e:
            log.error(f"Outlook COM Radar crashed: {e}")
        finally:
            pythoncom.CoUninitialize()

    t = threading.Thread(target=_run, daemon=True, name="OutlookRadar")
    t.start()
    return t
