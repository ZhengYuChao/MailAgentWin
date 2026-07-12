import pytest
import tempfile
import os
from pathlib import Path
from datetime import datetime
from src.models import Email, Attachment
from src.converter.eml_generator import EMLGenerator

def test_eml_generator_generate():
    email = Email(
        message_id="test-123",
        subject="Test Subject",
        sender="test@example.com",
        sender_name="Test User",
        to="to@example.com",
        content="<h1>Hello</h1>",
        content_type="text/html",
        date=datetime(2023, 1, 1, 12, 0)
    )
    
    with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tf:
        out_path = Path(tf.name)
        
    try:
        result_path = EMLGenerator.generate(email, output_path=out_path)
        
        import email as email_module
        assert result_path.exists()
        with open(result_path, 'r', encoding='utf-8') as f:
            content = f.read()
            msg = email_module.message_from_string(content)
            
        assert msg["Subject"] == "Test Subject"
        assert msg["From"] == "Test User <test@example.com>"
        assert msg["To"] == "to@example.com"
        assert msg["Message-ID"] == "test-123"
        html_part = msg.get_payload(0)
        assert "<h1>Hello</h1>" in html_part.get_payload(decode=True).decode('utf-8')
    finally:
        if out_path.exists():
            os.remove(out_path)

def test_eml_generator_with_attachment():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as att_file:
        att_file.write(b"attachment content")
        att_path = att_file.name
        
    try:
        att = Attachment(filename="test.txt", content_type="text/plain", size=18, path=att_path)
        email = Email(
            message_id="test-124",
            subject="Test With Attachment",
            sender="test@example.com",
            attachments=[att],
            content="Hello",
            content_type="text/plain"
        )
        
        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tf:
            out_path = Path(tf.name)
            
        try:
            EMLGenerator.generate(email, output_path=out_path)
            
            with open(out_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            assert "Content-Disposition: attachment; filename=test.txt" in content
            # base64 编码后的 "attachment content"
            assert "YXR0YWNobWVudCBjb250ZW50" in content
        finally:
            if out_path.exists():
                os.remove(out_path)
    finally:
        if os.path.exists(att_path):
            os.remove(att_path)
