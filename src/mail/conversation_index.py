"""
Outlook ConversationIndex 解析。
规则:
  - 头 22 字节 (44 hex chars) 是会话根的 FILETIME + GUID
  - 之后每 5 字节 (10 hex chars) 是一层回复
  - 父邮件 = 子邮件 ConversationIndex 去掉最后 10 hex
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

ROOT_LEN_HEX = 44   # 22 bytes
LEVEL_LEN_HEX = 10  # 5 bytes per level

@dataclass
class ConvIndex:
    raw: str
    root: str           # 前 44 hex
    depth: int          # 0 = 会话根
    parent_index: Optional[str]

def parse(conversation_index: str) -> Optional[ConvIndex]:
    if not conversation_index or len(conversation_index) < ROOT_LEN_HEX:
        return None
    ci = conversation_index.upper()
    depth = (len(ci) - ROOT_LEN_HEX) // LEVEL_LEN_HEX
    parent = ci[:-LEVEL_LEN_HEX] if depth > 0 else None
    return ConvIndex(raw=ci, root=ci[:ROOT_LEN_HEX], depth=depth, parent_index=parent)

def find_parent_in_db(child_index: str, sync_store) -> Optional[str]:
    """在本地 SQLite SyncStore 中按 ConversationIndex 前缀查父邮件的 Notion page URL。"""
    parsed = parse(child_index)
    if not parsed or not parsed.parent_index:
        return None
    # 假设 sync_store 实现了 get_by_conv_index 方法
    row = sync_store.get_by_conv_index(parsed.parent_index)
    return row["notion_page_url"] if row else None

# 单元测试样例
if __name__ == "__main__":
    # 正常长度通常是 44, 54, 64...
    root_idx = "01D9F2A4B8123456789012345678901234567890ABCD"  # 44 hex
    p0 = parse(root_idx)
    print(f"Root: depth={p0.depth}")
    
    child = root_idx + "AAAAAAAAAA"
    p1 = parse(child)
    print(f"Child: depth={p1.depth}, parent={p1.parent_index}")
    
    assert p1.depth == 1
    assert p1.parent_index == root_idx
    print("Test OK")
