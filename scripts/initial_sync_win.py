import asyncio
import sys
import os
from pathlib import Path

# 将项目根目录添加到 sys.path
root_dir = Path(__file__).parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from datetime import datetime, timedelta
from loguru import logger

from src.mail.outlook_com_arm import OutlookComArm
from src.mail.new_watcher_win import WindowsWatcher
from src.config import config

async def initial_sync(days: int = 7):
    logger.info(f"Starting initial sync for the last {days} days...")
    
    watcher = WindowsWatcher()
    since = datetime.now() - timedelta(days=days)
    
    # 扫描收件箱
    logger.info("Scanning Inbox...")
    inbox_entry_ids = watcher.arm.iter_folder(since=since)
    
    from tqdm import tqdm
    inbox_count = 0
    for eid, sid in tqdm(inbox_entry_ids, desc="Syncing Inbox", unit="mail"):
        await watcher.process_mail_sync(entry_id=eid, store_id=sid, trigger_ai=False)
        inbox_count += 1
            
    # 扫描发件箱
    logger.info("Scanning Sent Items...")
    # olFolderSentMail = 5
    sent_entry_ids = watcher.arm.iter_folder(folder_kind=5, since=since)

    sent_count = 0
    for eid, sid in tqdm(sent_entry_ids, desc="Syncing Sent", unit="mail"):
        await watcher.process_mail_sync(entry_id=eid, store_id=sid, trigger_ai=False)
        sent_count += 1

    await watcher.close()
    logger.info(f"Initial sync complete. Inbox: {inbox_count}, Sent: {sent_count}")

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    asyncio.run(initial_sync(days))
