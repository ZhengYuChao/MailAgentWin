import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from loguru import logger
from src.config import config
from src.scheduler.task_pool import global_task_pool
from src.models import TaskType, TaskPriority
from src.api.tunnel import global_tunnel_manager

def extract_property_text(prop_data):
    """Extract plain text from Notion property dictionary."""
    if not prop_data:
        return ""
    if prop_data.get("type") == "rich_text":
        return "".join([t.get("plain_text", "") for t in prop_data.get("rich_text", [])])
    elif prop_data.get("type") == "title":
        return "".join([t.get("plain_text", "") for t in prop_data.get("title", [])])
    elif prop_data.get("type") == "email":
        return prop_data.get("email") or ""
    elif prop_data.get("type") == "url":
        return prop_data.get("url") or ""
    elif prop_data.get("type") == "phone_number":
        return prop_data.get("phone_number") or ""
    return ""

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.handle_request()
        
    def do_GET(self):
        self.handle_request()

    def handle_request(self):
        client_ip, client_port = self.client_address
        
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)
        url_action = query_params.get("action", [""])[0].lower()
        
        # ── 1. Host Validation ────────────────────────────────────────────────
        host_header = self.headers.get('Host', '')
        logger.info(f"👉 Received incoming request from {client_ip}:{client_port} | Host: {host_header}")
        is_local = "localhost" in host_header or "127.0.0.1" in host_header
        
        # Check against the allowed host set by tunnel manager
        allowed_host = global_tunnel_manager.allowed_host_keyword
        if not is_local and allowed_host not in host_header:
            logger.warning(f"⛔ Rejected – Host '{host_header}' does not contain '{allowed_host}'.")
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        # ── 2. Read Body ────────────────────────────────────────────────────────
        content_length_str = self.headers.get('Content-Length')
        body = ""
        if content_length_str:
            try:
                content_length = int(content_length_str)
                body_bytes = self.rfile.read(content_length)
                body = body_bytes.decode("utf-8", errors="replace").strip()
            except Exception as e:
                logger.error(f"❌ Error reading body: {e}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Bad Request")
                return

        if not body:
            logger.warning("⚠️  Request body is empty, ignoring.")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Ignored: Empty body")
            return

        # ── 3. Payload Validation ──────────────────────────────────────────────
        try:
            data = json.loads(body)
            logger.info(f"📦 Raw Webhook Payload: {json.dumps(data, ensure_ascii=False)}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON data: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return
            
        action_id = data.get("source", {}).get("action_id", "N/A")
        logger.info(f"📥 Received Webhook from {client_ip}:{client_port}. action_id: {action_id}")
        
        # Database ID Validation (accept both email sync DB and new mail DB)
        try:
            expected_db_id = config.email_database_id.replace("-", "").lower()
            new_mail_db_id = config.new_mail_database_id.replace("-", "").lower() if config.new_mail_database_id else ""
            received_db_id = ""
            if "data" in data and "parent" in data["data"]:
                received_db_id = data["data"]["parent"].get("database_id", "")
            if not received_db_id:
                received_db_id = data.get("data", {}).get("database_id", "")

            clean_received_id = received_db_id.replace("-", "").lower()
            
            is_new_mail = False
            if clean_received_id == expected_db_id:
                logger.info(f"✅ Database ID validated (email sync DB): {clean_received_id}")
            elif new_mail_db_id and clean_received_id == new_mail_db_id:
                is_new_mail = True
                logger.info(f"✅ Database ID validated (new mail DB): {clean_received_id}")
            else:
                logger.error(f"⛔ Database ID mismatch! Expected: {expected_db_id} or {new_mail_db_id}, Received: {clean_received_id}")
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Database mismatch")
                return
        except Exception as e:
            is_new_mail = False
            logger.warning(f"⚠️ Database ID validation skipped due to error: {e}")

        properties = data.get("data", {}).get("properties", {})
        if not properties:
            logger.error("Invalid data format: Missing 'properties' field.")
            self.send_response(400)
            self.end_headers()
            return

        # ── New Mail flow (from new_mail database) ─────────────────────────────
        if is_new_mail:
            page_id = data.get("data", {}).get("id", "")
            # Subject may be named 'Subject' or 'Name' depending on database setup
            subject_prop = properties.get("Subject") or properties.get("Name")
            to_prop = properties.get("To")
            cc_prop = properties.get("CC More") or properties.get("CC")
            email_body_prop = properties.get("Email Body") or properties.get("HTMLBody")

            subject = extract_property_text(subject_prop).strip()
            to = extract_property_text(to_prop).strip()
            cc = extract_property_text(cc_prop).strip()
            email_body = extract_property_text(email_body_prop).strip() if email_body_prop else ""

            invalid_fields = []
            if not subject: invalid_fields.append("Subject")
            if not to: invalid_fields.append("To")

            if invalid_fields:
                logger.error(f"[NewMail] Validation failed: Fields are empty {', '.join(invalid_fields)}")
                self.send_response(400)
                self.end_headers()
                return

            logger.info(f"✅ [NewMail] Validation passed. Subject: {subject[:40]}, To: {to[:40]}, Email Body: {len(email_body)} chars")
            
            final_action = "new_mail"
            if url_action in ["new_mail_draft", "draft", "save", "create_draft"]:
                final_action = "new_mail_draft"
            elif url_action == "send":
                final_action = "new_mail"
                
            payload = {
                "action": final_action,
                "action_id": action_id,
                "subject": subject,
                "to": to,
                "cc_more": cc,
                "email_body": email_body,
                "page_id": page_id,
            }
            global_task_pool.add_task(TaskType.WEBHOOK_DRAFT, TaskPriority.HIGH, payload)

            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"OK: New mail task enqueued")
            return

        # ── Existing Reply/Forward/Draft flow ──────────────────────────────────
        message_id_prop = properties.get("Message ID")
        thread_id_prop = properties.get("Thread ID")
        reply_suggestion_prop = properties.get("Reply Suggestion")
        draft_action_prop = properties.get("Draft Action")
        from_prop = properties.get("From")
        to_prop = properties.get("To")
        cc_more_prop = properties.get("CC More") or properties.get("CC")

        message_id = extract_property_text(message_id_prop).strip()
        thread_id = extract_property_text(thread_id_prop).strip()
        reply_suggestion = extract_property_text(reply_suggestion_prop).strip()
        reply_to = extract_property_text(from_prop).strip()
        forward_to = extract_property_text(to_prop).strip()
        cc_more = extract_property_text(cc_more_prop).strip()

        invalid_fields = []
        if not message_id: invalid_fields.append("Message ID")
        if not thread_id: invalid_fields.append("Thread ID")

        if invalid_fields:
            logger.error(f"Validation failed: Fields are empty {', '.join(invalid_fields)}")
            self.send_response(400)
            self.end_headers()
            return
            
        # Determine Action
        final_action = "save"
        prop_action = extract_property_text(draft_action_prop).strip().lower() if draft_action_prop else ""
        
        if url_action:
            if url_action in ["reply", "reply_all", "forward", "save"]:
                final_action = url_action
            elif url_action in ["draft", "create_draft"]:
                final_action = "save"
            else:
                logger.error(f"❌ Unknown action in URL '{url_action}'. Aborting.")
                self.send_response(400)
                self.end_headers()
                return
        else:
            if prop_action == "create draft" or prop_action == "save":
                final_action = "save"
            elif prop_action in ["send draft", "reply all"]:
                final_action = "reply_all"
            elif prop_action == "reply":
                final_action = "reply"
            elif prop_action == "forward":
                final_action = "forward"
            else:
                logger.error(f"❌ Unknown action in Draft Action property '{prop_action}' and no URL action provided. Aborting.")
                self.send_response(400)
                self.end_headers()
                return

        # Reply Suggestion is required for reply/reply_all, but optional for forward/save
        if final_action in ("reply", "reply_all") and not reply_suggestion:
            logger.error(f"Validation failed: Reply Suggestion is empty (required for {final_action})")
            self.send_response(400)
            self.end_headers()
            return

        # ── 4. Enqueue Task (恢复优雅的任务池架构) ──────────────────────────────────
        logger.info(f"✅ Validation passed. Action: {final_action}, message_id: {message_id[:20]}...")
        payload = {
            "message_id": message_id,
            "thread_id": thread_id,
            "reply_suggestion": reply_suggestion,
            "action": final_action,
            "action_id": action_id,
            "reply_to": reply_to,
            "forward_to": forward_to,
            "cc_more": cc_more,
        }
        
        # 加入任务池（最高优先级 Priority 1），交由不阻塞的主循环去异步分发
        global_task_pool.add_task(TaskType.WEBHOOK_DRAFT, TaskPriority.HIGH, payload)

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"OK: Task enqueued")

    def _fetch_page_body_as_html(self, page_id: str) -> str:
        """Fetch Notion page blocks and convert to HTML for email body."""
        import requests
        
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        headers = {
            "Authorization": f"Bearer {config.notion_token}",
            "Notion-Version": "2022-06-28",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        blocks = resp.json().get("results", [])
        
        html_parts = []
        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            
            # Extract rich_text from block
            rich_texts = block_data.get("rich_text", [])
            text = "".join([rt.get("plain_text", "") for rt in rich_texts])
            
            if not text and block_type not in ("divider", "blank"):
                continue
                
            if block_type == "paragraph":
                html_parts.append(f"<p>{self._rich_text_to_html(rich_texts)}</p>")
            elif block_type in ("heading_1", "heading_2", "heading_3"):
                level = block_type[-1]
                html_parts.append(f"<h{level}>{self._rich_text_to_html(rich_texts)}</h{level}>")
            elif block_type == "bulleted_list_item":
                html_parts.append(f"<li>{self._rich_text_to_html(rich_texts)}</li>")
            elif block_type == "numbered_list_item":
                html_parts.append(f"<li>{self._rich_text_to_html(rich_texts)}</li>")
            elif block_type == "divider":
                html_parts.append("<hr>")
            elif block_type == "code":
                html_parts.append(f"<pre><code>{text}</code></pre>")
            else:
                # Fallback for other block types
                if text:
                    html_parts.append(f"<p>{self._rich_text_to_html(rich_texts)}</p>")
        
        return "\n".join(html_parts)
    
    def _rich_text_to_html(self, rich_texts: list) -> str:
        """Convert Notion rich_text array to HTML with formatting."""
        parts = []
        for rt in rich_texts:
            text = rt.get("plain_text", "")
            annotations = rt.get("annotations", {})
            href = rt.get("href")
            
            # Apply formatting
            if annotations.get("bold"):
                text = f"<b>{text}</b>"
            if annotations.get("italic"):
                text = f"<i>{text}</i>"
            if annotations.get("underline"):
                text = f"<u>{text}</u>"
            if annotations.get("strikethrough"):
                text = f"<s>{text}</s>"
            if annotations.get("code"):
                text = f"<code>{text}</code>"
            if href:
                text = f'<a href="{href}">{text}</a>'
            
            parts.append(text)
        return "".join(parts)

    def log_message(self, format, *args):
        # Override to suppress default HTTP logging
        pass

def start_api_server(port: int = 54321):
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"🔌 HTTP Server listening on 0.0.0.0:{port} ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("🛑 Server stopped.")
    finally:
        server.server_close()
