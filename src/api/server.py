import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    return ""

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.handle_request()
        
    def do_GET(self):
        self.handle_request()

    def handle_request(self):
        client_ip, client_port = self.client_address
        
        # ── 1. Host Validation ────────────────────────────────────────────────
        host_header = self.headers.get('Host', '')
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
            logger.debug(f"Raw Webhook Payload: {json.dumps(data, ensure_ascii=False)}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON data: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return
            
        action_id = data.get("source", {}).get("action_id", "N/A")
        logger.info(f"📥 Received Webhook from {client_ip}:{client_port}. action_id: {action_id}")
        
        # Database ID Validation
        try:
            expected_db_id = config.email_database_id.replace("-", "").lower()
            received_db_id = ""
            if "data" in data and "parent" in data["data"]:
                received_db_id = data["data"]["parent"].get("database_id", "")
            if not received_db_id:
                received_db_id = data.get("data", {}).get("database_id", "")

            clean_received_id = received_db_id.replace("-", "").lower()
            
            if clean_received_id != expected_db_id:
                logger.error(f"⛔ Database ID mismatch! Expected: {expected_db_id}, Received: {clean_received_id}")
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Database mismatch")
                return
            logger.info(f"✅ Database ID validated: {clean_received_id}")
        except Exception as e:
            logger.warning(f"⚠️ Database ID validation skipped due to error: {e}")

        properties = data.get("data", {}).get("properties", {})
        if not properties:
            logger.error("Invalid data format: Missing 'properties' field.")
            self.send_response(400)
            self.end_headers()
            return

        message_id_prop = properties.get("Message ID")
        thread_id_prop = properties.get("Thread ID")
        reply_suggestion_prop = properties.get("Reply Suggestion")
        draft_action_prop = properties.get("Draft Action")
        from_prop = properties.get("From")

        message_id = extract_property_text(message_id_prop).strip()
        thread_id = extract_property_text(thread_id_prop).strip()
        reply_suggestion = extract_property_text(reply_suggestion_prop).strip()
        reply_to = extract_property_text(from_prop).strip()

        invalid_fields = []
        if not message_id: invalid_fields.append("Message ID")
        if not thread_id: invalid_fields.append("Thread ID")
        if not reply_suggestion: invalid_fields.append("Reply Suggestion")

        if invalid_fields:
            logger.error(f"Validation failed: Fields are empty {', '.join(invalid_fields)}")
            self.send_response(400)
            self.end_headers()
            return
            
        # Determine Action
        CREATE_DRAFT_ACTION_ID = config.notion_action_create_draft
        SEND_DRAFT_ACTION_ID = config.notion_action_reply_all
        REPLY_ACTION_ID = config.notion_action_reply
        
        final_action = "save"
        prop_action = extract_property_text(draft_action_prop).strip().lower() if draft_action_prop else ""
        
        if action_id == CREATE_DRAFT_ACTION_ID:
            final_action = "save"
        elif action_id == SEND_DRAFT_ACTION_ID:
            final_action = "reply_all"
        elif action_id == REPLY_ACTION_ID:
            final_action = "reply"
        else:
            if prop_action == "create draft":
                final_action = "save"
            elif prop_action in ["send draft", "reply all"]:
                final_action = "reply_all"
            elif prop_action == "reply":
                final_action = "reply"
            else:
                logger.error(f"❌ Unknown action '{prop_action}'. Cannot determine if Send or Create Draft. Aborting. action_id={action_id}")
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
        }
        
        # 加入任务池（最高优先级 Priority 1），交由不阻塞的主循环去异步分发
        global_task_pool.add_task(TaskType.WEBHOOK_DRAFT, TaskPriority.HIGH, payload)

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"OK: Task enqueued")

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
