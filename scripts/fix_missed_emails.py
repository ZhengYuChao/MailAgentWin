"""
修复遗漏邮件同步脚本（一次性使用）

原因：Bug 修复前，Message-ID 为空的邮件会被 check_page_exists("") 误判为已存在，
导致跳过同步但仍在 SyncStore 中留下了错误记录（指向别人的 Notion 页面）。

本脚本做两件事：
  1. 清理 SyncStore 中 message_id 为空的错误记录
  2. 重新扫描最近 N 天的邮件，重新同步被遗漏的邮件

用法（在 Windows 机器上，项目根目录执行）：
  python scripts/fix_missed_emails.py          # 默认补查最近 7 天
  python scripts/fix_missed_emails.py 30       # 补查最近 30 天
  python scripts/fix_missed_emails.py --clean-only  # 仅清理，不重新同步
"""
import asyncio
import sys
import os
import sqlite3
from pathlib import Path

# 将项目根目录添加到 sys.path
root_dir = Path(__file__).parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from loguru import logger

# 配置日志
logger.remove()
fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | [FixMissed] {message}"
logger.add(sys.stderr, level="INFO", format=fmt)
logger.add("logs/fix_missed.log", rotation="10 MB", level="DEBUG", encoding="utf-8", format=fmt)


def clean_sync_store() -> int:
    """
    清理 SyncStore 中 message_id 为空的记录。
    
    这些记录是 Bug 造成的：空 Message-ID 的邮件被 check_page_exists("") 误判后，
    process_mail_sync 仍然保存了一条 sync record（指向错误的 Notion 页面）。
    
    清理后，这些邮件的 entry_id 不再被 is_synced() 拦截，可以重新同步。
    
    Returns:
        被清理的记录数
    """
    db_path = "data/sync_store.db"
    if not os.path.exists(db_path):
        logger.warning(f"SyncStore database not found: {db_path}")
        return 0
    
    with sqlite3.connect(db_path) as conn:
        # 先查看有多少条受影响的记录
        cursor = conn.execute(
            "SELECT COUNT(*) FROM mail_sync WHERE message_id IS NULL OR message_id = ''"
        )
        count = cursor.fetchone()[0]
        
        if count == 0:
            logger.info("✅ No empty Message-ID records found in SyncStore. Nothing to clean.")
            return 0
        
        # 列出受影响的记录（方便确认）
        cursor = conn.execute(
            "SELECT entry_id, message_id, notion_page_url, last_synced_at "
            "FROM mail_sync WHERE message_id IS NULL OR message_id = '' "
            "ORDER BY last_synced_at DESC"
        )
        rows = cursor.fetchall()
        
        logger.info(f"Found {count} SyncStore record(s) with empty Message-ID:")
        for row in rows[:20]:  # 最多显示 20 条
            eid_short = row[0][:20] + "..." if row[0] and len(row[0]) > 20 else row[0]
            logger.info(f"  entry_id={eid_short}, synced_at={row[3]}")
        if count > 20:
            logger.info(f"  ... and {count - 20} more")
        
        # 执行清理
        conn.execute(
            "DELETE FROM mail_sync WHERE message_id IS NULL OR message_id = ''"
        )
        logger.info(f"🗑️ Deleted {count} record(s) from SyncStore.")
        
    return count


async def resync_emails(days: int = 7):
    """重新扫描最近 N 天的邮件并同步遗漏的邮件"""
    from datetime import datetime, timedelta
    from src.mail.outlook_com_arm import OutlookComArm, OL_FOLDER_INBOX, OL_FOLDER_SENT
    from src.mail.new_watcher_win import WindowsWatcher

    logger.info(f"🔄 Starting re-sync for the last {days} days...")
    
    watcher = WindowsWatcher()
    since = datetime.now() - timedelta(days=days)
    
    # 扫描收件箱
    logger.info("Scanning Inbox...")
    inbox_items = watcher.arm.iter_folder(OL_FOLDER_INBOX, since=since)
    
    # 扫描发件箱
    logger.info("Scanning Sent Items...")
    sent_items = watcher.arm.iter_folder(OL_FOLDER_SENT, since=since)
    
    all_items = inbox_items + sent_items
    logger.info(f"Found {len(all_items)} total emails in the last {days} days.")
    
    # 过滤出未同步的
    from src.mail.sync_store import SyncStore
    sync_store = SyncStore()
    unsynced = [(eid, sid) for eid, sid in all_items if not sync_store.is_synced(eid)]
    
    if not unsynced:
        logger.info("✅ All emails are already synced. Nothing to do.")
        await watcher.close()
        return
    
    logger.info(f"📬 Found {len(unsynced)} unsynced emails. Starting sync...")
    
    from tqdm import tqdm
    success_count = 0
    fail_count = 0
    
    for eid, sid in tqdm(unsynced, desc="Re-syncing", unit="mail"):
        try:
            await watcher.process_mail_sync(entry_id=eid, store_id=sid, trigger_ai=False)
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to sync {eid[:20]}: {e}")
            fail_count += 1
    
    await watcher.close()
    logger.info(f"✅ Re-sync complete. Success: {success_count}, Failed: {fail_count}")


def main():
    clean_only = "--clean-only" in sys.argv
    
    # 解析天数参数
    days = 7
    for arg in sys.argv[1:]:
        if arg.isdigit():
            days = int(arg)
            break
    
    # Step 1: 清理错误的 SyncStore 记录
    logger.info("=" * 50)
    logger.info("Step 1: Cleaning up invalid SyncStore records...")
    logger.info("=" * 50)
    cleaned = clean_sync_store()
    
    if clean_only:
        logger.info("--clean-only mode. Skipping re-sync.")
        return
    
    # Step 2: 重新同步
    logger.info("")
    logger.info("=" * 50)
    logger.info(f"Step 2: Re-syncing emails from the last {days} days...")
    logger.info("=" * 50)
    asyncio.run(resync_emails(days=days))


if __name__ == "__main__":
    main()
