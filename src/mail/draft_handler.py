import pythoncom
import win32com.client
from loguru import logger
from src.config import config

def execute_draft_action(payload: dict):
    """
    通过 Outlook COM 执行发送或保存草稿。
    payload 包含: message_id, thread_id, reply_suggestion, action (send/save), action_id
    """
    message_id = payload.get("message_id")
    thread_id = payload.get("thread_id")
    reply_suggestion = payload.get("reply_suggestion")
    final_action = payload.get("action")
    action_id = payload.get("action_id")
    
    pythoncom.CoInitialize()
    try:
        try:
            target_account = config.mail_account_name
        except Exception as e:
            logger.warning(f"Failed to load config: {e}. Using default account.")
            target_account = ""

        try:
            import win32com.client.gencache
            app = win32com.client.gencache.EnsureDispatch("Outlook.Application")
        except Exception:
            app = win32com.client.Dispatch("Outlook.Application")
            
        ns = app.GetNamespace("MAPI")
        
        target_folder = None
        if target_account:
            stores = ns.Stores
            for i in range(1, stores.Count + 1):
                store = stores.Item(i)
                if target_account.lower() in store.DisplayName.lower():
                    root = store.GetRootFolder()
                    folders = root.Folders
                    for j in range(1, folders.Count + 1):
                        folder = folders.Item(j)
                        if folder.DefaultItemType == 0 and folder.Name.lower() in ["收件箱", "inbox"]:
                            target_folder = folder
                            logger.info(f"Found target Inbox in store: {store.DisplayName}")
                            break
                    if target_folder:
                        break

        if not target_folder:
            logger.info("Using default Inbox folder.")
            target_folder = ns.GetDefaultFolder(6) # olFolderInbox
            
        # 确保 message_id 带有 < 和 > 以匹配 Outlook 的 PR_INTERNET_MESSAGE_ID 格式
        if message_id and not message_id.startswith("<"):
            message_id = f"<{message_id}"
        if message_id and not message_id.endswith(">"):
            message_id = f"{message_id}>"
        
        query = f"@SQL=\"http://schemas.microsoft.com/mapi/proptag/0x1035001F\" = '{message_id}'"
        items = target_folder.Items.Restrict(query)
        
        target_item = None
        if items.Count > 0:
            target_item = items.Item(1)
            logger.info("Email found by Message ID in Inbox.")
        else:
            logger.warning("Email not found by Message ID in Inbox. Attempting to search by ConversationID...")
            conv_query = f"@SQL=\"http://schemas.microsoft.com/mapi/proptag/0x71CA0102\" = '{thread_id}'"
            conv_items = target_folder.Items.Restrict(conv_query)
            if conv_items.Count > 0:
                target_item = conv_items.Item(conv_items.Count)
                logger.info("Email found by Thread ID in Inbox.")
            else:
                logger.info("Email not found in Inbox. Quickly checking Sent Items...")
                try:
                    sent_folder = ns.GetDefaultFolder(5) # olFolderSentMail
                    sent_items = sent_folder.Items.Restrict(query)
                    if sent_items.Count > 0:
                        target_item = sent_items.Item(1)
                        logger.info("Email found by Message ID in Sent Items.")
                    else:
                        sent_conv_items = sent_folder.Items.Restrict(conv_query)
                        if sent_conv_items.Count > 0:
                            target_item = sent_conv_items.Item(sent_conv_items.Count)
                            logger.info("Email found by Thread ID in Sent Items.")
                except Exception as e:
                    logger.error(f"Error checking Sent Items: {e}")
                    
                if not target_item:
                    logger.info("Email not found in Inbox or Sent Items, searching all folders in the account (this may take a while)...")
                def search_folder(folder):
                    try:
                        fitems = folder.Items
                        res = fitems.Restrict(query)
                        if res.Count > 0: return res.Item(1)
                        res2 = fitems.Restrict(conv_query)
                        if res2.Count > 0: return res2.Item(res2.Count)
                    except Exception: pass
                    for i in range(1, folder.Folders.Count + 1):
                        found = search_folder(folder.Folders.Item(i))
                        if found: return found
                    return None
                
                if target_folder and target_folder.Parent:
                    target_item = search_folder(target_folder.Parent)
                    if target_item: logger.info("Email found in another folder.")

        if not target_item:
            logger.error("Could not find the corresponding email in any folder.")
            return

        try:
            eid = target_item.EntryID
            target_item = ns.GetItemFromID(eid)
            logger.info(f"Re-fetched item by EntryID.")
        except Exception as e:
            logger.warning(f"Failed to re-fetch item by EntryID: {e}")

        reply_to = payload.get("reply_to")
        reply = None
        
        if final_action == "reply":
            try:
                logger.info("Calling target_item.Reply()...")
                reply = target_item.Reply()
                if reply_to:
                    reply.To = reply_to
                    logger.info(f"Overriding To field with: {reply_to}")
            except Exception as e:
                logger.warning(f"Reply() failed with exception: {e}")
        else: # "reply_all" or "save"
            try:
                logger.info("Calling target_item.ReplyAll()...")
                reply = target_item.ReplyAll()
            except Exception as e:
                logger.warning(f"ReplyAll() failed with exception: {e}")
                
            if reply is None:
                logger.warning("ReplyAll() returned None or failed, attempting Reply()...")
                try:
                    reply = target_item.Reply()
                except Exception as e:
                    logger.warning(f"Reply() failed with exception: {e}")
            
        if reply is None:
            logger.warning("Reply() also failed. Creating a NEW mail item as fallback...")
            try:
                reply = app.CreateItem(0)
                import re
                orig_subject = target_item.Subject
                clean_subject = re.sub(r'^([Rr][Ee]:\s*)+', '', orig_subject).strip()
                reply.Subject = f"RE: {clean_subject}"
                
                reply.To = reply_to if reply_to else target_item.SenderEmailAddress
                reply.Body = reply_suggestion + "\n\n--- Original Message ---\n" + getattr(target_item, "Body", "")
                logger.info("Created new MailItem as fallback.")
            except Exception as e:
                logger.error(f"Failed to create new MailItem fallback: {e}")
                return
        else:
            reply.Body = reply_suggestion + "\n\n" + getattr(reply, "Body", "")

        # 3. Final Action: Send or Save
        if final_action == "save":
            reply.Save()
            logger.info(f"✅ Draft created successfully (action_id: {action_id}).")
        else: # "reply" or "reply_all"
            reply.Send()
            logger.info(f"✅ Email sent successfully (action_id: {action_id}).")

    except Exception as e:
        logger.error(f"Failed to process Outlook email: {e}")
    finally:
        pythoncom.CoUninitialize()
