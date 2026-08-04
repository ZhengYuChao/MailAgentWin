with open('src/mail/new_watcher_win.py', 'r', encoding='utf-8') as f:
    content = f.read()

import re

# Find the start of process_mail_sync
start_match = re.search(r'    async def process_mail_sync\(.*?:', content)
if not start_match:
    print("Could not find process_mail_sync")
    exit(1)

start_idx = start_match.start()

# Find the start of the next method _notify_ai_trigger
end_match = re.search(r'    def _notify_ai_trigger\(self\):', content)
if not end_match:
    print("Could not find _notify_ai_trigger")
    exit(1)

end_idx = end_match.start()

new_content = content[:start_idx] + """    async def process_mail_sync(self, entry_id: str, store_id: Optional[str] = None, trigger_ai: bool = True):
        \"\"\"处理同步任务：获取邮件 -> 转换内容 -> 写入 Notion -> (可选) 触发 AI\"\"\"
        import time as _time
        _sync_start = _time.time()
        _short_eid = entry_id[:32]
        logger.info(f"🔍 [DEDUP-TRACE] ===== START process_mail_sync entry_id={_short_eid} =====")

        # === Layer 1: Atomic SQLite claim ===
        claimed = self.sync_store.try_claim(entry_id)
        if not claimed:
            logger.info(f"⏭️ [DEDUP-L1] Entry already claimed/synced: {_short_eid}")
            return
        logger.info(f"✅ [DEDUP-L1] Claimed entry_id={_short_eid} (new claim)")

        fetched = self.arm.fetch_by_entry_id(entry_id, store_id)
        if not fetched:
            logger.warning(f"Could not fetch email from Outlook: {_short_eid}")
            self.sync_store.release_claim(entry_id)
            return

        logger.info(f"📧 [DEDUP-TRACE] Fetched email: subject='{fetched.subject[:60]}', "
                    f"message_id={fetched.message_id[:60] if fetched.message_id else 'NONE'}, "
                    f"entry_id={_short_eid}, mailbox={fetched.mailbox}")

        msg_lock = None
        if fetched.message_id:
            msg_lock = await self._get_msg_lock(fetched.message_id)
            await msg_lock.acquire()

        try:
            # === Layer 2: Cross-EntryID dedup by Message-ID ===
            if fetched.message_id:
                existing = self.sync_store.get_by_message_id(fetched.message_id)
                if existing and existing.get('entry_id') != entry_id:
                    logger.info(f"⏭️ [DEDUP-L2] Email already synced under different EntryID "
                                f"(Message-ID: {fetched.message_id[:60]})")
                    self.sync_store.link_entry_id(entry_id, existing)
                    return
                elif existing:
                    logger.info(f"ℹ️ [DEDUP-L2] Same entry_id found in SyncStore")
                else:
                    logger.info(f"✅ [DEDUP-L2] Message-ID not in SyncStore, proceeding.")
            else:
                logger.warning(f"⚠️ [DEDUP-L2] No Message-ID for entry_id={_short_eid} — L2 skipped")

            email = Email(
                message_id=fetched.message_id,
                subject=fetched.subject,
                sender=fetched.from_email,
                sender_name=fetched.from_name,
                to="; ".join(fetched.to),
                cc="; ".join(fetched.cc),
                date=datetime.fromisoformat(fetched.date_utc),
                content=fetched.html_body or fetched.text_body,
                content_type="text/html" if fetched.html_body else "text/plain",
                mailbox=fetched.mailbox,
                is_read=fetched.is_read,
                is_flagged=fetched.is_flagged,
                has_attachments=fetched.has_attachments,
                thread_id=fetched.conversation_id,
                in_reply_to=fetched.in_reply_to,
                internal_id=None,
            )

            if fetched.has_attachments:
                try:
                    raw_item = self.arm.get_raw_item(entry_id)
                    if raw_item:
                        attachments = self.attachment_handler.extract(raw_item)
                        for att in attachments:
                            email.attachments.append(Attachment(
                                filename=att.filename,
                                content_type=att.content_type,
                                size=att.size,
                                path=att.local_path,
                                content_id=att.content_id,
                                is_inline=att.is_inline
                            ))
                except Exception as e:
                    logger.warning(f"⚠️ Failed to extract attachments for '{email.subject}': {e}")

            parent_page_url = find_parent_in_db(fetched.conversation_index, self.sync_store)
            
            # === Layer 3: Notion-level dedup (check_page_exists inside create_email_page_v2) ===
            logger.info(f"🔄 [DEDUP-L3] Calling create_email_page_v2 for message_id={fetched.message_id[:60] if fetched.message_id else 'NONE'}")
            page_id = await self.notion_sync.create_email_page_v2(email)
            _elapsed = _time.time() - _sync_start
            
            if page_id:
                notion_url = f"https://notion.so/{page_id.replace('-', '')}"
                
                # 保存同步记录
                self.sync_store.save_sync_record(
                    entry_id=entry_id,
                    message_id=fetched.message_id,
                    conversation_id=fetched.conversation_id,
                    conversation_index=fetched.conversation_index,
                    notion_page_url=notion_url,
                    notion_page_id=page_id,
                    parent_page_url=parent_page_url or ""
                )
                logger.info(f"💾 [DEDUP-TRACE] Saved sync record: entry_id={_short_eid}, "
                            f"notion_page_id={page_id}")
                
                if fetched.mailbox == "Inbox":
                    await self.feishu.notify_important_email({
                        "subject": email.subject,
                        "from_name": email.sender_name,
                        "from_email": email.sender,
                        "date": email.date.isoformat(),
                        "page_id": page_id,
                        "mailbox": fetched.mailbox,
                        "ai_priority": "🔵 一般", 
                        "ai_action": "查阅",
                        "ai_summary": email.content[:200] + "..."
                    })
                    
                logger.info(f"✅ [DEDUP-TRACE] Successfully uploaded to Notion: '{email.subject[:50]}' "
                            f"(entry={_short_eid}, page_id={page_id}, elapsed={_elapsed:.1f}s)")
                
                self.arm.mark_as_read(entry_id)
                
                if trigger_ai:
                    self._notify_ai_trigger()
            else:
                logger.warning(f"⚠️ [DEDUP-L3] create_email_page_v2 returned None "
                               f"(likely already exists in Notion). entry_id={_short_eid}")
                self.sync_store.release_claim(entry_id)

        except Exception as e:
            self.sync_store.release_claim(entry_id)
            logger.error(f"❌ [DEDUP-TRACE] Failed to sync email: entry_id={_short_eid}, error={e}")
        finally:
            if msg_lock:
                msg_lock.release()

""" + content[end_idx:]

with open('src/mail/new_watcher_win.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Replacement successful.")
