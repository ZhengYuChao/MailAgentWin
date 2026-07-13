"""
订阅 Outlook NewMailEx 事件 + 兜底轮询，统一吐 EntryID 到全局任务池。
COM 事件回调里只做最轻量动作，绝对禁止发 HTTP / 调 Notion。
"""
import threading, time, logging, os, subprocess
import pythoncom, win32com.client
from src.scheduler.task_pool import global_task_pool
from src.models import TaskType, TaskPriority

log = logging.getLogger(__name__)

# Outlook COM 常见可重试错误码
_CALL_REJECTED = -2147418111     # RPC_E_CALL_REJECTED: Outlook 正忙/初始化中
_RPC_FAILED = -2147023170        # RPC_S_CALL_FAILED: RPC 连接失败
_RPC_UNAVAILABLE = -2147023174   # RPC_S_SERVER_UNAVAILABLE
_NOT_CONNECTED = -2147220995     # MAPI 未连接

_BUSY_ERRORS = {_CALL_REJECTED, _RPC_FAILED}
_FATAL_ERRORS = {_RPC_UNAVAILABLE, _NOT_CONNECTED}


def _extract_hresult(exc: Exception) -> int:
    """从 COM 异常中提取 HRESULT 错误码"""
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int):
        return args[0]
    # 尝试从字符串中提取
    for known in (_CALL_REJECTED, _RPC_FAILED, _RPC_UNAVAILABLE, _NOT_CONNECTED):
        if str(known) in str(exc):
            return known
    return 0


def _wait_for_outlook_ready(max_wait: int = 60, interval: float = 3.0) -> bool:
    """
    用轻量级 Dispatch 探测 Outlook 是否已就绪。
    在尝试 DispatchWithEvents 之前调用，避免在 Outlook 初始化阶段
    触发事件订阅导致 RPC_E_CALL_REJECTED。
    
    Returns: True 如果 Outlook 就绪，False 如果超时仍未就绪
    """
    start = time.time()
    attempt = 0
    while time.time() - start < max_wait:
        attempt += 1
        try:
            app = win32com.client.Dispatch("Outlook.Application")
            ns = app.GetNamespace("MAPI")
            # 尝试一个轻量级操作来确认 MAPI 真正可用
            _ = ns.GetDefaultFolder(6)  # olFolderInbox
            log.info(f"Outlook COM ready (probe took {attempt} attempt(s), "
                     f"{time.time() - start:.1f}s)")
            return True
        except Exception as e:
            hr = _extract_hresult(e)
            if hr in _BUSY_ERRORS:
                log.debug(f"Outlook busy (attempt {attempt}): {e}")
            else:
                log.debug(f"Outlook not ready (attempt {attempt}): {e}")
            time.sleep(interval)
    
    log.warning(f"Outlook readiness probe timed out after {max_wait}s")
    return False


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
            restarted_once = False
            fail_start_time = None
            retry_backoff = 5  # 初始重试等待秒数
            
            # 启动前先探测 Outlook 是否就绪
            log.info("Waiting for Outlook to be ready before starting COM Radar...")
            _wait_for_outlook_ready(max_wait=90, interval=3.0)
            
            while True:
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
                    
                    # 成功连接后，重置失败状态
                    fail_start_time = None
                    retry_backoff = 5
                    
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
                    hr = _extract_hresult(e)
                    now = time.time()
                    
                    if fail_start_time is None:
                        fail_start_time = now
                    elif now - fail_start_time > 180: # 3分钟 = 180秒
                        log.error(f"Outlook COM Radar failed continuously for 3 minutes. Giving up. Last error: {e}")
                        break
                    
                    if hr in _BUSY_ERRORS:
                        # Outlook 正忙/正在初始化 → 只等待重试，不 kill 重启
                        log.warning(f"Outlook COM Radar: Outlook is busy ({e}). "
                                    f"Waiting {retry_backoff}s before retry...")
                        time.sleep(retry_backoff)
                        retry_backoff = min(retry_backoff * 1.5, 30)  # 递增但不超过30秒
                        continue
                        
                    log.error(f"Outlook COM Radar crashed/rejected: {e}.")
                    
                    if not restarted_once:
                        log.info("Attempting to restart Outlook (Only once)...")
                        try:
                            subprocess.run(["taskkill", "/F", "/IM", "OUTLOOK.EXE"], capture_output=True)
                            time.sleep(2)
                            shortcut_path = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Outlook (classic).lnk"
                            if os.path.exists(shortcut_path):
                                os.startfile(shortcut_path)
                            else:
                                os.startfile("outlook")
                            restarted_once = True
                            log.info("Outlook restarted, waiting for it to be ready...")
                            # 重启后用探测器等待就绪，而不是固定 sleep
                            _wait_for_outlook_ready(max_wait=60, interval=3.0)
                        except Exception as restart_err:
                            log.error(f"Failed to restart Outlook: {restart_err}")
                            time.sleep(10)
                    else:
                        log.info(f"Already restarted Outlook once. Waiting {retry_backoff}s before next retry...")
                        time.sleep(retry_backoff)
                        retry_backoff = min(retry_backoff * 1.5, 30)
        finally:
            pythoncom.CoUninitialize()

    t = threading.Thread(target=_run, daemon=True, name="OutlookRadar")
    t.start()
    return t

