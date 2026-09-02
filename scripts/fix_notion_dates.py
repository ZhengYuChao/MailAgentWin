"""
修复 Notion 邮件时间戳脚本 (Fix Notion Email Dates)

原因：在时区自动检测修复前，邮件时间戳曾被错误附加了 Pacific Time (-07:00) 时区，
导致在中国时区 (+08:00) 的 Notion 中，当天下午的邮件被显示为 "Tomorrow"（明天）。

本脚本做的事：
  1. 扫描 Outlook 中最近 N 天（默认 14 天）的收件箱和发件箱邮件
  2. 提取准确的本地时间并转换为正确的系统时区 ISO 格式（+08:00）
  3. 找到对应的 Notion 页面，直接更新其 Date 属性
  4. 既不会删除页面，也不会创建重复页面！

用法（在 Windows 电脑上，项目根目录执行）：
  python scripts/fix_notion_dates.py        # 默认修复最近 14 天的邮件时间
  python scripts/fix_notion_dates.py 30     # 修复最近 30 天的邮件时间
"""
import asyncio
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

# 将项目根目录添加到 sys.path
root_dir = Path(__file__).parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from loguru import logger
from src.config import config, get_system_local_tz
from src.mail.outlook_com_arm import OutlookComArm, OL_FOLDER_INBOX, OL_FOLDER_SENT
from src.mail.sync_store import SyncStore
from src.notion.client import NotionClient

# 日志配置
logger.remove()
fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | [FixDates] {message}"
logger.add(sys.stderr, level="INFO", format=fmt)
logger.add("logs/fix_dates.log", rotation="10 MB", level="DEBUG", encoding="utf-8", format=fmt)


async def fix_dates(days: int = 14):
    local_tz = get_system_local_tz()
    logger.info("=" * 60)
    logger.info(f"🕒 启动 Notion 邮件时间修复工具")
    logger.info(f"📍 当前系统检测时区: {local_tz}")
    logger.info(f"📅 修复范围: 最近 {days} 天")
    logger.info("=" * 60)

    arm = OutlookComArm()
    sync_store = SyncStore()
    notion_client = NotionClient()

    since = datetime.now() - timedelta(days=days)

    logger.info("正在扫描 Outlook 收件箱...")
    inbox_items = arm.iter_folder(OL_FOLDER_INBOX, since=since)
    logger.info(f"收件箱找到 {len(inbox_items)} 封邮件")

    logger.info("正在扫描 Outlook 已发送邮件...")
    sent_items = arm.iter_folder(OL_FOLDER_SENT, since=since)
    logger.info(f"已发送文件夹找到 {len(sent_items)} 封邮件")

    all_items = inbox_items + sent_items
    logger.info(f"共计 {len(all_items)} 封邮件待检查")

    updated_count = 0
    skipped_count = 0
    failed_count = 0

    from tqdm import tqdm

    for eid, sid in tqdm(all_items, desc="修复时间戳", unit="mail"):
        # 1. 查询 SyncStore 获取 Notion Page ID
        notion_page_id = sync_store.get_page_id(eid)
        
        # 2. 从 Outlook 抓取邮件详情获取原始时间
        try:
            fetched = arm.get_mail_by_id(eid, sid)
            if not fetched:
                continue

            correct_iso = fetched.date_utc  # 已包含正确的 +08:00 时区信息
            if not correct_iso:
                continue

            # 如果 SyncStore 没找到 page_id，尝试按 Message-ID 在 Notion 中搜索
            if not notion_page_id and fetched.message_id:
                try:
                    notion_page_id = await notion_client.find_page_by_message_id(fetched.message_id)
                except Exception:
                    pass

            if not notion_page_id:
                skipped_count += 1
                continue

            # 3. 更新 Notion 页面的 Date 属性
            await notion_client.client.pages.update(
                page_id=notion_page_id,
                properties={
                    "Date": {"date": {"start": correct_iso}}
                }
            )
            updated_count += 1
            logger.debug(f"✅ 已更新 '{fetched.subject[:30]}': Date -> {correct_iso}")

        except Exception as e:
            failed_count += 1
            logger.error(f"❌ 修复失败 (entry_id={eid[:20]}): {e}")

    await notion_client.close()
    arm.close()

    logger.info("=" * 60)
    logger.info(f"🎉 修复完成！")
    logger.info(f"✅ 成功更新: {updated_count} 封邮件的 Notion 时间")
    logger.info(f"⏭️ 未同步到 Notion 的邮件跳过: {skipped_count} 封")
    if failed_count > 0:
        logger.warning(f"⚠️ 更新失败: {failed_count} 封")
    logger.info("=" * 60)


def main():
    days = 14
    for arg in sys.argv[1:]:
        if arg.isdigit():
            days = int(arg)
            break
    asyncio.run(fix_dates(days=days))


if __name__ == "__main__":
    main()
