import pytest
from src.converter.html_converter import HTMLToNotionConverter

def test_is_meaningful_text():
    converter = HTMLToNotionConverter()
    
    # 正常文本
    assert converter._is_meaningful_text("Hello World") == True
    assert converter._is_meaningful_text("中文内容") == True
    
    # 空白文本
    assert converter._is_meaningful_text("") == False
    assert converter._is_meaningful_text("   ") == False
    assert converter._is_meaningful_text("\n\t") == False
    
    # 隐形字符
    assert converter._is_meaningful_text("\u200b\u200c") == False
    assert converter._is_meaningful_text(" \u00a0 ") == False

def test_truncate_by_utf16():
    converter = HTMLToNotionConverter()
    
    # 短文本不截断
    short_text = "This is a short text."
    assert converter._truncate_by_utf16(short_text, 100) == short_text
    
    # 长文本截断（以纯英文字符为例，utf-16编码后长度翻倍，但这里的max_length是按字符数量计算的代理）
    # 假设 max_length=5，即最多5个字符
    long_text = "HelloWorld"
    truncated = converter._truncate_by_utf16(long_text, 5)
    assert len(truncated) <= 5
    assert truncated == "Hell"
