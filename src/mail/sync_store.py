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

    def try_claim(self, entry_id: str) -> bool:
        """Atomically claim an entry_id for processing.
        Uses INSERT OR IGNORE to prevent race conditions across threads.
        Returns True if successfully claimed (new), False if already claimed/synced."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO mail_sync (entry_id) VALUES (?)",
                    (entry_id,)
                )
                claimed = cur.rowcount > 0
                if not claimed:
                    # Log what already exists for this entry_id
                    existing = conn.execute(
                        "SELECT entry_id, message_id, notion_page_id FROM mail_sync WHERE entry_id = ?",
                        (entry_id,)
                    ).fetchone()
                    logger.debug(f"[DEDUP-L1] try_claim REJECTED {entry_id[:32]}: "
                                 f"existing_row=({existing[0][:24] if existing else 'None'}, "
                                 f"mid={existing[1][:40] if existing and existing[1] else 'None'}, "
                                 f"npid={existing[2][:16] if existing and existing[2] else 'None'})")
                else:
                    logger.debug(f"[DEDUP-L1] try_claim ACCEPTED {entry_id[:32]}")
                return claimed
        except Exception as e:
            logger.error(f"try_claim failed for {entry_id[:24]}: {e}")
            return False

    def release_claim(self, entry_id: str):
        """Release a claimed entry_id on processing failure (allows fallback retry).
        Only deletes records without notion_page_id (i.e., uncompleted claims)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM mail_sync WHERE entry_id = ? AND (notion_page_id IS NULL OR notion_page_id = '')",
                    (entry_id,)
                )
        except Exception as e:
            logger.error(f"release_claim failed for {entry_id[:24]}: {e}")

    def link_entry_id(self, entry_id: str, existing_record: Dict[str, Any]):
        """Link a new entry_id to an already-synced email (cross-EntryID dedup).
        Saves a record without message_id to avoid UNIQUE constraint conflict."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO mail_sync 
                    (entry_id, conversation_id, conversation_index, 
                     notion_page_url, notion_page_id, parent_page_url, last_synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                """, (entry_id,
                      existing_record.get('conversation_id', ''),
                      existing_record.get('conversation_index', ''),
                      existing_record.get('notion_page_url', ''),
                      existing_record.get('notion_page_id', ''),
                      existing_record.get('parent_page_url', '')))
                logger.info(f"🔗 Linked entry {entry_id[:24]} to existing Notion page "
                           f"{existing_record.get('notion_page_id', '')[:16]}")
        except Exception as e:
            logger.error(f"link_entry_id failed: {e}")
