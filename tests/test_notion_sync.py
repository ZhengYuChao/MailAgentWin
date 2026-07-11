import pytest
from datetime import datetime
from src.models import Email
from src.notion.sync import NotionSync

def test_sanitize_text():
    # 测试文本过滤敏感信息
    assert NotionSync._sanitize_text("/etc/hosts") == "/etc/\u200Bhosts"
    assert NotionSync._sanitize_text("/etc/passwd") == "/etc/\u200Bpasswd"
    assert NotionSync._sanitize_text("normal text /etc/something else") == "normal text /etc/something else"

def test_build_properties():
    sync = NotionSync()
    email_date = datetime(2023, 1, 1, 12, 0)
    email = Email(
        message_id="msg-1",
        subject="Test Subject",
        sender="sender@example.com",
        to="to@example.com",
        cc="cc@example.com",
        date=email_date,
        mailbox="Inbox"
    )
    
    props = sync._build_properties(email)
    
    assert props["Subject"]["title"][0]["text"]["content"] == "Test Subject"
    assert props["From"]["email"] == "sender@example.com"
    assert props["To"]["rich_text"][0]["text"]["content"] == "to@example.com"
    assert props["CC"]["rich_text"][0]["text"]["content"] == "cc@example.com"
    assert props["Is Read"]["checkbox"] == False
    assert props["Mailbox"]["select"]["name"] == "Inbox"
    assert "Thread ID" not in props
    
    # Test with Thread ID and EML file upload
    email.thread_id = "thread-1"
    props_with_thread = sync._build_properties(email, eml_file_upload_id="file-1")
    assert props_with_thread["Thread ID"]["rich_text"][0]["text"]["content"] == "thread-1"
    assert props_with_thread["Original EML"]["files"][0]["file_upload"]["id"] == "file-1"
