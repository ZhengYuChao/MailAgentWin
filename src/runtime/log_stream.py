"""
Cursor-based log reader with ANSI strip and token redaction.
Used by GET /api/logs and GET /api/logs/stream (SSE).
"""
from __future__ import annotations
import re
import os
from pathlib import Path
from loguru import logger


# Patterns to strip / redact
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mKHJGF]')
_TOKEN_RE = re.compile(r'ntn_\S+')
_BEARER_RE = re.compile(r'Bearer\s+ntn_\S+')


def _get_active_log_file() -> Path | None:
    """Find the actively written log file."""
    from src.setup.persistence import get_log_path
    primary = get_log_path()
    root_fallback = Path(__file__).parent.parent.parent / "logs" / "mailagent.log"

    candidates = [p for p in (primary, root_fallback) if p.exists() and p.stat().st_size > 0]
    if not candidates:
        return primary if primary.exists() else (root_fallback if root_fallback.exists() else primary)

    # Return the one modified most recently
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _sanitize(line: str) -> str:
    """Strip ANSI codes and redact Notion tokens."""
    line = _ANSI_RE.sub('', line)
    line = _BEARER_RE.sub('Bearer [REDACTED]', line)
    line = _TOKEN_RE.sub('[REDACTED]', line)
    return line


def read_lines(
    cursor: int = -1,
    max_lines: int = 250,
    worker_filter: str = "",
    search: str = "",
) -> tuple[list[str], int]:
    """
    Read log lines.
    If cursor < 0 or cursor == 0 (initial request):
      Reads the tail of the log file (up to `max_lines` latest lines).
    If cursor > 0:
      Reads newly appended lines since `cursor`.
    Returns (lines, new_cursor).
    """
    path = _get_active_log_file()
    if not path or not path.exists():
        return [], 0

    try:
        stat = os.stat(path)
        file_size = stat.st_size

        if file_size == 0:
            return [], 0

        # Initial fetch: tail the last max_lines
        if cursor <= 0:
            # Read from up to 256KB from the end
            seek_pos = max(0, file_size - 256 * 1024)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(seek_pos)
                if seek_pos > 0:
                    f.readline()  # discard partial first line
                raw_lines = f.readlines()

            lines = [_sanitize(l.rstrip('\r\n')) for l in raw_lines]
            if len(lines) > max_lines:
                lines = lines[-max_lines:]

            # Apply filters
            if worker_filter:
                lines = [l for l in lines if f"[{worker_filter}]".lower() in l.lower()]
            if search:
                lines = [l for l in lines if search.lower() in l.lower()]

            return lines, file_size

        # Incremental fetch: read new lines from cursor
        if cursor > file_size:
            # Log file rotated or truncated -> re-tail
            return read_lines(cursor=-1, max_lines=max_lines, worker_filter=worker_filter, search=search)

        if cursor == file_size:
            return [], file_size

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(cursor)
            raw_lines = f.readlines()
            new_cursor = f.tell()

        lines = [_sanitize(l.rstrip('\r\n')) for l in raw_lines]
        if worker_filter:
            lines = [l for l in lines if f"[{worker_filter}]".lower() in l.lower()]
        if search:
            lines = [l for l in lines if search.lower() in l.lower()]

        return lines, new_cursor

    except Exception as e:
        logger.error(f"log_stream.read_lines failed: {e}")
        return [], cursor


def tail_new_lines(cursor: int) -> tuple[list[str], int]:
    """Return only lines added since `cursor`."""
    return read_lines(cursor=cursor, max_lines=500)
