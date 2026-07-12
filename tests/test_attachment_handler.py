import pytest
from src.mail.attachment_handler import AttachmentHandler

def test_sanitize_filename():
    assert AttachmentHandler._sanitize_filename('valid_name.txt') == 'valid_name.txt'
    assert AttachmentHandler._sanitize_filename('invalid<name>.txt') == 'invalid_name_.txt'
    assert AttachmentHandler._sanitize_filename('bad:"/\\|?*name.txt') == 'bad_______name.txt'
    
    # 测试超长截断
    long_name = "a" * 250 + ".txt"
    sanitized = AttachmentHandler._sanitize_filename(long_name)
    assert len(sanitized) == 200
