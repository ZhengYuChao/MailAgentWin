import sqlite3
import os
from typing import Optional, Dict, Any
from loguru import logger

class SyncStore:
    """持久化同步状态存储 (SQLite)"""

    def __init__(self, db_path: str = "data/sync_store.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_sync (
                entry_id           TEXT PRIMARY KEY,
                message_id         TEXT UNIQUE,
                conversation_id    TEXT,
                conversation_index TEXT,
                notion_page_url    TEXT,
                notion_page_id     TEXT,
                parent_page_url    TEXT,
                last_synced_at     TEXT DEFAULT (datetime('now', 'localtime'))
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_index ON mail_sync(conversation_index);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_id ON mail_sync(conversation_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_id ON mail_sync(message_id);")

    def save_sync_record(self, entry_id: str, message_id: str, 
                         conversation_id: str = "", conversation_index: str = "",
                         notion_page_url: str = "", notion_page_id: str = "",
                         parent_page_url: str = ""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            INSERT OR REPLACE INTO mail_sync 
            (entry_id, message_id, conversation_id, conversation_index, notion_page_url, notion_page_id, parent_page_url, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """, (entry_id, message_id or None, conversation_id, conversation_index, notion_page_url, notion_page_id, parent_page_url))

    def get_by_entry_id(self, entry_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM mail_sync WHERE entry_id = ?", (entry_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        if not message_id: return None
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM mail_sync WHERE message_id = ?", (message_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_by_conv_index(self, conv_index: str) -> Optional[Dict[str, Any]]:
        if not conv_index: return None
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM mail_sync WHERE conversation_index = ?", (conv_index,))
            row = cur.fetchone()
            return dict(row) if row else None

    def is_synced(self, entry_id: str) -> bool:
        return self.get_by_entry_id(entry_id) is not None
