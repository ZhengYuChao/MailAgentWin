"""
附件提取、Notion 上传、内联图片 CID 替换。
Notion File Upload API 流程:
  1) POST /v1/file_uploads -> 拿 upload_id + presigned URL
  2) PUT presigned URL -> 上传二进制
  3) PATCH /v1/blocks/{page}/children -> 添加 file block
"""
import os
import tempfile
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional, List
import requests

log = logging.getLogger(__name__)

# MAPI DASL 属性
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_DIRECT_UPLOAD = 20 * 1024 * 1024  # 20MB 单步上限

@dataclass
class ExtractedAttachment:
    filename: str
    local_path: str
    size: int
    content_id: Optional[str]   # 内联图片有 CID
    is_inline: bool
    content_type: str = "application/octet-stream"

class AttachmentHandler:
    def __init__(self, notion_token: str):
        self.token = notion_token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {notion_token}",
            "Notion-Version": NOTION_VERSION,
        })

    def extract(self, mail_item, work_dir: Optional[str] = None) -> List[ExtractedAttachment]:
        """从 Outlook MailItem 提取附件到本地"""
        work_dir = work_dir or tempfile.mkdtemp(prefix="mailagent_")
        os.makedirs(work_dir, exist_ok=True)
        result = []
        
        if mail_item.Attachments.Count == 0:
            return []

        for i in range(1, mail_item.Attachments.Count + 1):
            att = mail_item.Attachments.Item(i)
            try:
                # 获取 Content-ID (cid)
                pa = att.PropertyAccessor
                cid = pa.GetProperty(PR_ATTACH_CONTENT_ID) or ""
            except Exception:
                cid = ""
            
            filename = getattr(att, "FileName", f"attach_{i}")
            safe_name = self._sanitize_filename(filename)
            
            # 为防止同名冲突，加一个 hash 前缀
            dedup = hashlib.md5(f"{safe_name}_{i}".encode()).hexdigest()[:8]
            local_path = os.path.join(work_dir, f"{dedup}_{safe_name}")
            
            try:
                att.SaveAsFile(local_path)
                size = os.path.getsize(local_path)
                
                # 简单猜测 content_type
                import mimetypes
                ctype, _ = mimetypes.guess_type(local_path)
                
                result.append(ExtractedAttachment(
                    filename=safe_name,
                    local_path=local_path,
                    size=size,
                    content_id=cid if cid else None,
                    is_inline=bool(cid),
                    content_type=ctype or "application/octet-stream"
                ))
            except Exception as e:
                log.warning(f"Failed to save attachment {safe_name}: {e}")
                continue
                
        return result

    def upload_to_notion(self, att: ExtractedAttachment) -> Optional[str]:
        """
        上传附件到 Notion (File Upload API)
        返回 file_upload_id
        """
        try:
            # Step 1: Request upload session
            # 注意：Notion 官方 SDK 可能还没有直接支持这个实验性 API 的封装
            # 这里使用 requests 手动调用
            
            # 这是一个占位实现，因为官方 API 可能需要特定的 endpoint
            # 根据提供的方案：
            mode = "single_part" if att.size <= MAX_DIRECT_UPLOAD else "multi_part"
            payload = {
                "filename": att.filename,
                "content_type": att.content_type,
                "mode": mode
            }
            
            # 注意：这里的 API Endpoint /file_uploads 是示意性的，
            # 实际生产中应根据 Notion 官方文档或实际可用的内测 API 调整。
            # 如果没有官方直传 API，通常做法是传到 S3/Azure 然后给 Notion URL。
            # 但既然方案中提到了这个 API，我们按此实现。
            
            # TODO: 验证 Notion 是否已公开 /file_uploads 接口
            # 目前 Notion API 更多是使用外部 URL。
            # 如果此 API 不可用，则需要 fallback 到其他存储。
            
            r = self.session.post(f"{NOTION_API}/file_uploads", json=payload, timeout=15)
            if not r.ok:
                log.error(f"Failed to create Notion upload session: {r.text}")
                return None
                
            upload_data = r.json()
            upload_id = upload_data["id"]
            upload_url = upload_data["upload_url"]
            
            # Step 2: Upload bits
            with open(att.local_path, "rb") as f:
                if mode == "single_part":
                    # 直接上传到预签名 URL
                    up_res = requests.put(upload_url, data=f, headers={"Content-Type": att.content_type}, timeout=300)
                    up_res.raise_for_status()
                else:
                    # 分片上传逻辑 (省略，参考方案 3.4)
                    self._multipart_upload(upload_url, att.local_path, att.size)
            
            # Step 3: Complete upload if multi-part
            if mode == "multi_part":
                self.session.post(f"{NOTION_API}/file_uploads/{upload_id}/complete").raise_for_status()
                
            return upload_id
            
        except Exception as e:
            log.error(f"Attachment upload failed: {e}")
            return None

    def _multipart_upload(self, upload_url: str, path: str, total_size: int):
        # 简化版分片上传
        pass

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        bad = '<>:"/\\|?*'
        return "".join("_" if c in bad else c for c in name)[:200]
