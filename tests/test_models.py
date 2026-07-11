import pytest
from datetime import datetime
from src.models import Email, Attachment, CalendarEvent, Attendee

def test_email_post_init():
    # 测试 Email dataclass 的初始化逻辑
    email = Email(
        message_id="12345",
        subject="",  # 应该被替换为 (No Subject)
        sender="test@example.com"
        # sender_name 应该被推断为 test
    )
    
    assert email.subject == "(No Subject)"
    assert email.sender_name == "test"
    assert email.has_attachments == False

    email_with_att = Email(
        message_id="12345",
        subject="Hello",
        sender="test@example.com",
        attachments=[Attachment(filename="test.txt", content_type="text/plain", size=10, path="/tmp/test.txt")]
    )
    assert email_with_att.has_attachments == True

def test_email_missing_message_id():
    with pytest.raises(ValueError, match="message_id is required"):
        Email(message_id="", subject="Test", sender="test@example.com")

def test_calendar_event():
    event = CalendarEvent(
        event_id="evt1",
        calendar_name="Work",
        title="",
        start_time=datetime.now(),
        end_time=datetime.now(),
        attendees=[
            Attendee(email="a@example.com", name="Alice", status="accepted"),
            Attendee(email="b@example.com", status="tentative")
        ]
    )
    
    assert event.title == "(无标题)"
    assert event.attendee_count == 2
    assert "Alice(accepted)" in event.attendees_str
    assert "b@example.com(tentative)" in event.attendees_str
