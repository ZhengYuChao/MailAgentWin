from typing import Dict, Any, List, Set, Optional, TYPE_CHECKING
from pathlib import Path
from loguru import logger
from datetime import datetime, timezone, timedelta
import re
import shutil

if TYPE_CHECKING:
    from src.mail.icalendar_parser import MeetingInvite

from src.models import Email, Attachment
from src.notion.client import NotionClient, FileSizeLimitError
from src.converter.html_converter import HTMLToNotionConverter
from src.converter.eml_generator import EMLGenerator
from src.converter.office_converter_win import convert_office_attachment, is_convertible

BEIJING_TZ = timezone(timedelta(hours=8))

class NotionSync:
    """Notion 同步器"""

    def __init__(self):
        self.client = NotionClient()
        self.html_converter = HTMLToNotionConverter()
        self.eml_generator = EMLGenerator()

    async def close(self):
        """关闭客户端会话"""
        await self.client.close()

    async def sync_email(self, email: Email) -> bool:
        """同步邮件到 Notion（兼容旧 API）"""
        page_id = await self.create_email_page_v2(email)
        return page_id is not None

    async def _upload_attachments(self, email: Email) -> "tuple[List[Dict[str, Any]], List[str]]":
        """上传邮件附件到 Notion"""
        uploaded_attachments = []
        failed_filenames = []

        if not email.attachments:
            return uploaded_attachments, failed_filenames

        logger.info(f"Email contains {len(email.attachments)} attachments, starting upload...")
        from tqdm import tqdm

        for attachment in tqdm(email.attachments, desc="Uploading Attachments", unit="file"):
            try:
                file_upload_id = await self.client.upload_file(attachment.path)
                uploaded_attachments.append({
                    'filename': attachment.filename,
                    'file_upload_id': file_upload_id,
                    'content_type': attachment.content_type,
                    'size': attachment.size,
                    'content_id': attachment.content_id,
                    'is_inline': attachment.is_inline
                })
            except FileSizeLimitError as e:
                logger.warning(f"\n  Skipped uploading {attachment.filename}: {e}")
                failed_filenames.append(attachment.filename)
            except Exception as e:
                logger.error(f"\n  Failed to upload {attachment.filename}: {e}")
                failed_filenames.append(attachment.filename)

        if failed_filenames:
            logger.warning(f"Failed to upload {len(failed_filenames)} attachments: {failed_filenames}")

        return uploaded_attachments, failed_filenames

    def _convert_office_attachments(self, email: Email) -> List[Attachment]:
        """将 Office 附件转换为更通用的格式"""
        converted_attachments = []
        convertible = [a for a in email.attachments if is_convertible(a.filename)]
        if not convertible:
            return converted_attachments

        logger.info(f"Found {len(convertible)} convertible Office attachments")

        for attachment in convertible:
            try:
                output_dir = str(Path(attachment.path).parent)
                converted_paths = convert_office_attachment(attachment.path, output_dir)
                for converted_path in converted_paths:
                    p = Path(converted_path)
                    ext = p.suffix.lower()
                    content_type = "application/pdf" if ext == ".pdf" else "text/csv"
                    converted_attachments.append(Attachment(
                        filename=p.name,
                        content_type=content_type,
                        size=p.stat().st_size,
                        path=str(p),
                        content_id=None,
                        is_inline=False,
                    ))
            except Exception as e:
                logger.warning(f"Failed to convert {attachment.filename}, skipping: {e}")

        if converted_attachments:
            logger.info(f"Generated {len(converted_attachments)} converted attachments: "
                        f"{[a.filename for a in converted_attachments]}")
        return converted_attachments

    async def _upload_eml_file(self, email: Email) -> Optional[str]:
        """生成并上传 .eml 归档文件"""
        try:
            eml_path = self.eml_generator.generate(email)
            logger.debug(f"Generated .eml file: {eml_path.name}")
            
            try:
                # 默认直接上传 .eml 文件
                file_upload_id = await self.client.upload_file(str(eml_path))
                logger.info(f"Uploaded email file: {eml_path.name}")
                return file_upload_id
            except Exception as e:
                error_str = str(e)
                # 当遇到 403 错误（通常是 Cloudflare WAF 拦截代码片段）时，退避使用 zip 压缩再上传
                if "403" in error_str or "Cloudflare" in error_str or "blocked" in error_str.lower():
                    logger.warning(f"⚠️ EML upload blocked by WAF (403), falling back to zip upload for {eml_path.name}...")
                    import zipfile
                    import os
                    zip_path = eml_path.with_suffix('.eml.zip')
                    try:
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            zf.write(eml_path, eml_path.name)
                            
                        file_upload_id = await self.client.upload_file(str(zip_path))
                        logger.info(f"Uploaded email zip file as fallback: {zip_path.name}")
                        return file_upload_id
                    finally:
                        try:
                            os.remove(zip_path)
                        except Exception:
                            pass
                else:
                    raise e
                    
        except FileSizeLimitError as e:
            logger.warning(f"Skipped uploading email file: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to generate/upload email file: {e}")
            return None

    async def _create_page_with_blocks(self, properties: Dict[str, Any], children: List[Dict[str, Any]], icon: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """创建 Notion 页面，处理超过 100 blocks 的情况"""
        if len(children) <= 100:
            return await self.client.create_page(properties=properties, children=children, icon=icon)

        logger.info(f"Email contains {len(children)} blocks, creating in batches...")
        page = await self.client.create_page(properties=properties, children=children[:100], icon=icon)
        page_id = page['id']
        logger.info(f"Created page with first 100 blocks")

        remaining_blocks = children[100:]
        batch_size = 100
        for i in range(0, len(remaining_blocks), batch_size):
            batch = remaining_blocks[i:i + batch_size]
            await self.client.append_block_children(page_id, batch)
            logger.info(f"Appended {len(batch)} blocks (batch {i//batch_size + 1})")
        return page

    def _create_meeting_callout(self, invite: 'MeetingInvite') -> Dict[str, Any]:
        """创建会议邀请 Callout Block"""
        start = invite.start_time.astimezone(BEIJING_TZ)
        end = invite.end_time.astimezone(BEIJING_TZ)
        if invite.is_all_day:
            time_str = start.strftime("%Y-%m-%d") + " (全天)"
        else:
            time_str = f"{start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%H:%M')} (北京时间)"

        if invite.method == "CANCEL" or invite.status == "cancelled":
            title_prefix = "【会议已取消】"
            callout_color = "red_background"
        elif invite.sequence > 0:
            title_prefix = "【更新】"
            callout_color = "blue_background"
        else:
            title_prefix = ""
            callout_color = "blue_background"

        title_text = f"{title_prefix}在线会议邀请"
        lines = [f"📌 主题：{invite.summary}", f"🕐 时间：{time_str}"]
        if invite.location:
            lines.append(f"📍 地点：{invite.location}")
        content_text = "\n".join(lines)

        rich_text_parts = [
            {"type": "text", "text": {"content": title_text + "\n\n"}, "annotations": {"bold": True}},
            {"type": "text", "text": {"content": content_text}}
        ]

        if invite.teams_url:
            rich_text_parts.append({"type": "text", "text": {"content": "\n🔗 会议链接：输出"}})
            rich_text_parts.append({
                "type": "text",
                "text": {
                    "content": invite.teams_url[:80] + ("..." if len(invite.teams_url) > 80 else ""),
                    "link": {"url": invite.teams_url}
                },
                "annotations": {"color": "blue"}
            })

        if invite.meeting_id:
            rich_text_parts.append({"type": "text", "text": {"content": f"\n🆔 会议 ID：{invite.meeting_id}"}})
        if invite.passcode:
            rich_text_parts.append({"type": "text", "text": {"content": f"\n🔑 密码：{invite.passcode}"}})

        return {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": rich_text_parts,
                "icon": {"type": "emoji", "emoji": "🗓"},
                "color": callout_color
            }
        }

    def _build_image_map(self, email: Email, uploaded_attachments: List[Dict]) -> Dict[str, tuple]:
        """构建图片映射"""
        image_map = {}
        if email.content_type != "text/html":
            return image_map
        cid_pattern = r'cid:([^"\'\s>]+)'
        cid_matches = set(re.findall(cid_pattern, email.content, re.IGNORECASE))
        if not cid_matches:
            return image_map

        cid_to_upload_info = {}
        for att in uploaded_attachments:
            content_id = att.get('content_id')
            if content_id:
                content_type = att.get('content_type', 'application/octet-stream')
                upload_info = (att['file_upload_id'], content_type)
                cid_to_upload_info[content_id] = upload_info
                image_map[att['filename']] = upload_info

        for cid in cid_matches:
            if cid in cid_to_upload_info:
                image_map[cid] = cid_to_upload_info[cid]
            else:
                for att in uploaded_attachments:
                    content_id = att.get('content_id')
                    if content_id: continue
                    filename = att['filename']
                    filename_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename
                    cid_clean = cid.split('@')[0] if '@' in cid else cid
                    if (cid in filename or filename in cid or cid_clean in filename or filename_without_ext in cid):
                        upload_info = (att['file_upload_id'], att.get('content_type', 'application/octet-stream'))
                        image_map[cid] = upload_info
                        image_map[filename] = upload_info
                        break
        return image_map

    def _build_properties(self, email: Email, eml_file_upload_id: str = None) -> Dict[str, Any]:
        """构建 Notion Page Properties"""
        email_date = email.date
        if email_date.tzinfo is None:
            email_date = email_date.replace(tzinfo=BEIJING_TZ)
        else:
            email_date = email_date.astimezone(BEIJING_TZ)

        properties = {
            "Subject": {"title": [{"text": {"content": email.subject[:2000]}}]},
            "From": {"email": email.sender},
            "From Name": {"rich_text": [{"text": {"content": (email.sender_name or "")[:1999]}}]},
            "To": {"rich_text": [{"text": {"content": email.to[:1999]}}]} if email.to else {"rich_text": []},
            "CC": {"rich_text": [{"text": {"content": email.cc[:1999]}}]} if email.cc else {"rich_text": []},
            "Date": {"date": {"start": email_date.isoformat()}},
            "Message ID": {"rich_text": [{"text": {"content": email.message_id[:1999]}}]},
            "Processing Status": {"select": {"name": "未处理"}},
            "Is Read": {"checkbox": email.is_read},
            "Is Flagged": {"checkbox": email.is_flagged},
            "Has Attachments": {"checkbox": email.has_attachments},
            "Mailbox": {"select": {"name": email.mailbox}},
        }
        if email.thread_id:
            properties["Thread ID"] = {"rich_text": [{"text": {"content": email.thread_id[:1999]}}]}
        if email.internal_id:
            properties["ID"] = {"number": email.internal_id}
        if eml_file_upload_id:
            properties["Original EML"] = {"files": [{"type": "file_upload", "file_upload": {"id": eml_file_upload_id}}]}
        return properties

    _SENSITIVE_PATH_PATTERN = re.compile(r'/etc/(?=hosts|passwd|shadow|sudoers|crontab|fstab|resolv)')
    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        return cls._SENSITIVE_PATH_PATTERN.sub('/etc/\u200B', text)
    @classmethod
    def _sanitize_rich_text_list(cls, rich_text_list: list):
        for rt in rich_text_list:
            text_obj = rt.get('text', {})
            if 'content' in text_obj:
                text_obj['content'] = cls._sanitize_text(text_obj['content'])
    @classmethod
    def _sanitize_blocks(cls, blocks: list):
        for block in blocks:
            btype = block.get('type', '')
            container = block.get(btype, {})
            if isinstance(container, dict):
                if 'rich_text' in container: cls._sanitize_rich_text_list(container['rich_text'])
                for cell in container.get('cells', []): cls._sanitize_rich_text_list(cell)
                if 'children' in container: cls._sanitize_blocks(container['children'])

    def _build_children(self, email: Email, uploaded_attachments: List[Dict] = None, image_map: Dict[str, tuple] = None, meeting_invite: 'MeetingInvite' = None) -> List[Dict[str, Any]]:
        """构建 Notion Page Children"""
        children = []
        if meeting_invite:
            children.append(self._create_meeting_callout(meeting_invite))
            children.append({"object": "block", "type": "divider", "divider": {}})

        non_image_attachments = []
        inline_image_filenames = set(image_map.keys()) if image_map else set()
        if uploaded_attachments:
            for attachment in uploaded_attachments:
                content_type = attachment.get('content_type', '').lower()
                if not content_type.startswith('image/') or attachment['filename'] not in inline_image_filenames:
                    non_image_attachments.append(attachment)

        if non_image_attachments:
            children.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": "📎 附件"}}]}})
            for attachment in non_image_attachments:
                atype = "image" if attachment.get('content_type', '').lower().startswith('image/') else "file"
                children.append({"object": "block", "type": atype, atype: {"type": "file_upload", "file_upload": {"id": attachment['file_upload_id']}, "caption": [{"text": {"content": attachment['filename']}}]}})
            children.append({"object": "block", "type": "divider", "divider": {}})

        children.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "📧 邮件内容"}}]}})
        try:
            children.extend(self.html_converter.convert(email.content, image_map))
        except Exception as e:
            children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": email.content[:2000]}}]}})
        return children

    async def _find_thread_parent_by_thread_id(self, thread_id: Optional[str]) -> Optional[str]:
        if not thread_id: return None
        try:
            results = await self.client.query_database(filter_conditions={"property": "Message ID", "rich_text": {"equals": thread_id}})
            return results[0].get("id") if results else None
        except Exception: return None

    async def _find_all_thread_members_with_date(self, thread_id: str, exclude_message_id: str = None) -> List[Dict[str, Any]]:
        if not thread_id: return []
        try:
            ds_id = await self.client.get_data_source_id(self.client.email_db_id)
            results = await self.client.client.data_sources.query(data_source_id=ds_id, filter={"property": "Thread ID", "rich_text": {"equals": thread_id}}, page_size=100)
            thread_members = []
            for page in results.get("results", []):
                props = page.get("properties", {})
                msg_id_texts = props.get("Message ID", {}).get("rich_text", [])
                msg_id = msg_id_texts[0].get("text", {}).get("content", "") if msg_id_texts else ""
                if exclude_message_id and msg_id == exclude_message_id: continue
                date_prop = props.get("Date", {}).get("date", {})
                thread_members.append({"page_id": page.get("id"), "message_id": msg_id, "date": date_prop.get("start", "") if date_prop else ""})
            return thread_members
        except Exception: return []

    async def update_sub_items(self, page_id: str, child_page_ids: List[str]) -> bool:
        if not child_page_ids: return True
        try:
            valid_child_ids = list(set(pid for pid in child_page_ids if pid and pid != page_id))
            if not valid_child_ids: return True
            await self.client.client.pages.update(page_id=page_id, properties={"Parent item": {"relation": []}})
            await self.client.client.pages.update(page_id=page_id, properties={"Sub-item": {"relation": [{"id": pid} for pid in valid_child_ids]}})
            return True
        except Exception as e:
            logger.error(f"Error updating sub_items for page {page_id}: {e}")
            return False

    async def create_email_page_v2(self, email: Email, skip_parent_lookup: bool = False, calendar_page_id: str = None, meeting_invite: 'MeetingInvite' = None) -> Optional[str]:
        try:
            logger.info(f"Creating email page (v2): {email.subject}")
            if await self.client.check_page_exists(email.message_id):
                existing = await self.client.query_database(filter_conditions={"property": "Message ID", "rich_text": {"equals": email.message_id}})
                return existing[0].get("id") if existing else None
            
            from src.config import config as app_config
            if app_config.office_convert_enabled:
                import asyncio
                converted = await asyncio.to_thread(self._convert_office_attachments, email)
                if converted: email.attachments.extend(converted)

            uploaded_attachments, failed_attachments = await self._upload_attachments(email)
            eml_file_upload_id = await self._upload_eml_file(email)
            properties = self._build_properties(email, eml_file_upload_id)
            if calendar_page_id: properties["Calendar Events"] = {"relation": [{"id": calendar_page_id}]}
            image_map = self._build_image_map(email, uploaded_attachments)
            children = self._build_children(email, uploaded_attachments, image_map, meeting_invite)
            self._sanitize_blocks(children)

            if failed_attachments:
                children.insert(0, {"type": "callout", "callout": {"rich_text": [{"type": "text", "text": {"content": f"⚠️ {len(failed_attachments)} 个附件上传失败: {', '.join(failed_attachments)}"}}], "icon": {"type": "emoji", "emoji": "⚠️"}, "color": "yellow_background"}})

            email_icon = {"type": "emoji", "emoji": "📤"} if email.mailbox == "发件箱" else {"type": "emoji", "emoji": "📧"}
            page = await self._create_page_with_blocks(properties, children, email_icon)
            page_id = page['id']
            if not skip_parent_lookup and email.thread_id: await self._handle_thread_relations(page_id, email)
            return page_id
        except Exception: raise

    def _parse_date_to_beijing(self, date_str: str) -> Optional[datetime]:
        if not date_str: return None
        try:
            normalized = re.sub(r'\.\d+', '', date_str)
            return datetime.fromisoformat(normalized).astimezone(BEIJING_TZ)
        except Exception: return None

    async def _handle_thread_relations(self, page_id: str, email: Email):
        try:
            thread_members = []
            if email.thread_id:
                thread_members = await self._find_all_thread_members_with_date(email.thread_id, exclude_message_id=email.message_id)
            
            if thread_members:
                current_dt = (email.date.replace(tzinfo=BEIJING_TZ) if email.date.tzinfo is None else email.date.astimezone(BEIJING_TZ)) if email.date else None
                for member in thread_members: member['date_dt'] = self._parse_date_to_beijing(member.get('date', ''))
                valid_members = [m for m in thread_members if m.get('date_dt')]
                if valid_members:
                    latest_member = max(valid_members, key=lambda x: x['date_dt'])
                    if current_dt and current_dt >= latest_member['date_dt']:
                        await self.update_sub_items(page_id, [m['page_id'] for m in thread_members])
                    else:
                        latest_page_id = latest_member['page_id']
                        all_non_latest = [m['page_id'] for m in thread_members if m['page_id'] != latest_page_id]
                        all_non_latest.append(page_id)
                        await self.update_sub_items(latest_page_id, all_non_latest)
                    return

            # Fallback 1: Message-ID (In-Reply-To)
            in_reply_to = getattr(email, 'in_reply_to', None)
            if in_reply_to:
                parent_results = await self.client.query_database(filter_conditions={"property": "Message ID", "rich_text": {"equals": in_reply_to}})
                if parent_results:
                    parent_page_id = parent_results[0].get("id")
                    await self.update_parent_item(page_id, parent_page_id)
                    return
            
            # Fallback 2: Subject (Removed prefixes)
            if email.subject:
                import re
                clean_subject = re.sub(r'^((Re|Fwd|回复|转发|答复|FW|AW|RV)\s*:\s*)+', '', email.subject, flags=re.IGNORECASE).strip()
                if clean_subject and clean_subject != email.subject.strip():
                    subj_results = await self.client.query_database(filter_conditions={"property": "Subject", "title": {"equals": clean_subject}})
                    if subj_results:
                        parent_page_id = subj_results[0].get("id")
                        await self.update_parent_item(page_id, parent_page_id)
                        return

        except Exception as e:
            logger.error(f"Error handling thread relations for {email.message_id}: {e}")

    async def update_parent_item(self, page_id: str, parent_page_id: str) -> bool:
        try:
            await self.client.client.pages.update(page_id=page_id, properties={"Parent item": {"relation": [{"id": parent_page_id}]}})
            return True
        except Exception as e:
            logger.error(f"Error updating parent_item for page {page_id}: {e}")
            return False

    async def query_all_message_ids(self) -> Set[str]:
        message_ids = set()
        try:
            ds_id = await self.client.get_data_source_id(self.client.email_db_id)
            has_more, cursor = True, None
            while has_more:
                query_params = {"data_source_id": ds_id, "filter": {"property": "Message ID", "rich_text": {"is_not_empty": True}}, "page_size": 100}
                if cursor: query_params["start_cursor"] = cursor
                results = await self.client.client.data_sources.query(**query_params)
                for page in results.get("results", []):
                    rt = page.get("properties", {}).get("Message ID", {}).get("rich_text", [])
                    if rt and rt[0].get("text", {}).get("content"): message_ids.add(rt[0]["text"]["content"])
                has_more, cursor = results.get("has_more", False), results.get("next_cursor")
            return message_ids
        except Exception: return message_ids

    async def query_all_row_ids(self) -> Set[int]:
        row_ids = set()
        try:
            ds_id = await self.client.get_data_source_id(self.client.email_db_id)
            has_more, cursor = True, None
            while has_more:
                query_params = {"data_source_id": ds_id, "filter": {"property": "Row ID", "number": {"is_not_empty": True}}, "page_size": 100}
                if cursor: query_params["start_cursor"] = cursor
                results = await self.client.client.data_sources.query(**query_params)
                for page in results.get("results", []):
                    val = page.get("properties", {}).get("Row ID", {}).get("number")
                    if val is not None: row_ids.add(int(val))
                has_more, cursor = results.get("has_more", False), results.get("next_cursor")
            return row_ids
        except Exception: return row_ids

    async def query_pages_for_reverse_sync(self) -> List[Dict]:
        pages = []
        try:
            ds_id = await self.client.get_data_source_id(self.client.email_db_id)
            has_more, cursor = True, None
            while has_more:
                query_params = {"data_source_id": ds_id, "filter": {"and": [{"property": "Processing Status", "select": {"equals": "AI Reviewed"}}, {"property": "Synced to Mail", "checkbox": {"equals": False}}]}, "page_size": 100}
                if cursor: query_params["start_cursor"] = cursor
                results = await self.client.client.data_sources.query(**query_params)
                for page in results.get("results", []):
                    props = page.get("properties", {})
                    pages.append({
                        "page_id": page["id"],
                        "message_id": props.get("Message ID", {}).get("rich_text", [{}])[0].get("text", {}).get("content", ""),
                        "ai_action": props.get("Action Type", {}).get("select", {}).get("name", ""),
                        "subject": props.get("Subject", {}).get("title", [{}])[0].get("text", {}).get("content", ""),
                        "from_name": props.get("From Name", {}).get("rich_text", [{}])[0].get("text", {}).get("content", ""),
                        "from_email": props.get("From", {}).get("email", "") or "",
                        "to_addr": "".join(t.get("text", {}).get("content", "") for t in props.get("To", {}).get("rich_text", [])),
                        "cc_addr": "".join(t.get("text", {}).get("content", "") for t in props.get("CC", {}).get("rich_text", [])),
                        "date": props.get("Date", {}).get("date", {}).get("start", ""),
                        "ai_priority": props.get("Priority", {}).get("select", {}).get("name", ""),
                        "mailbox": props.get("Mailbox", {}).get("select", {}).get("name", ""),
                        "ai_summary": "".join(t.get("text", {}).get("content", "") for t in props.get("AI Summary", {}).get("rich_text", [])),
                        "row_id": props.get("ID", {}).get("number"),
                        "category": props.get("Category", {}).get("select", {}).get("name", ""),
                        "reply_suggestion": "".join(t.get("text", {}).get("content", "") for t in props.get("Reply Suggestion", {}).get("rich_text", [])),
                    })
                has_more, cursor = results.get("has_more", False), results.get("next_cursor")
            return pages
        except Exception: return pages

    async def update_page_mail_sync_status(self, page_id: str, synced: bool = True, processing_status: str = ""):
        try:
            props = {"Synced to Mail": {"checkbox": synced}}
            if processing_status: props["Processing Status"] = {"select": {"name": processing_status}}
            await self.client.client.pages.update(page_id=page_id, properties=props)
        except Exception: raise

    async def update_email_flags(self, page_id: str, is_read: bool, is_flagged: bool, processing_status: str = ""):
        try:
            props = {"Is Read": {"checkbox": is_read}, "Is Flagged": {"checkbox": is_flagged}}
            if processing_status: props["Processing Status"] = {"select": {"name": processing_status}}
            await self.client.client.pages.update(page_id=page_id, properties=props)
        except Exception: raise

    async def query_by_row_id(self, row_id: int) -> Optional[Dict]:
        try:
            results = await self.client.query_database(filter_conditions={"property": "Row ID", "number": {"equals": row_id}})
            return {"page_id": results[0]["id"], "row_id": row_id} if results else None
        except Exception: return None
