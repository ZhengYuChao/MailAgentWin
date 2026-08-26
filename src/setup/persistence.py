"""
Atomic config persistence for MailAgent.
Reads/writes MailAgentConfig to JSON at the user data root.
Never stores the Notion token or any secret — those live in dpapi_store.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
from pathlib import Path
from loguru import logger

from src.setup.schema import MailAgentConfig


def _data_root() -> Path:
    """Return platform-appropriate user data directory."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            return Path(base) / "MailAgent"
    # Mac / dev fallback
    return Path(__file__).parent.parent.parent / "data" / "MailAgent"


DATA_ROOT: Path = _data_root()
CONFIG_DIR: Path = DATA_ROOT / "config"
CONFIG_PATH: Path = CONFIG_DIR / "config.json"
BACKUP_PATH: Path = CONFIG_DIR / "config.json.bak"


def ensure_dirs() -> None:
    """Create all required directories under DATA_ROOT."""
    for subdir in ("config", "secrets", "browser", "state", "logs"):
        (DATA_ROOT / subdir).mkdir(parents=True, exist_ok=True)


PROJECT_ROOT: Path = Path(__file__).parent.parent.parent
PROJECT_CONFIG_PATH: Path = PROJECT_ROOT / "config.json"
PROMPT_DEFAULT_PATH: Path = PROJECT_ROOT / "prompt.txt"
PROMPT_DAILY_PATH: Path = PROJECT_ROOT / "prompt_daily.txt"


def load_config() -> MailAgentConfig:
    """
    Load MailAgentConfig from config.json.
    Returns a default (unconfigured) config if the file does not exist.
    """
    ensure_dirs()
    target = CONFIG_PATH if CONFIG_PATH.exists() else (PROJECT_CONFIG_PATH if PROJECT_CONFIG_PATH.exists() else None)
    cfg = None
    if target:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            cfg = MailAgentConfig(**data)
        except Exception as e:
            logger.error(f"Failed to load config.json from {target} ({e}). Trying backup…")
            if BACKUP_PATH.exists():
                try:
                    data = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
                    logger.warning("Loaded config from backup.")
                    cfg = MailAgentConfig(**data)
                except Exception as e2:
                    logger.error(f"Backup also failed ({e2}). Returning defaults.")

    if cfg is None:
        cfg = MailAgentConfig()

    # Prefill default prompts from root files if empty
    if not cfg.prompt_default and PROMPT_DEFAULT_PATH.exists():
        try:
            cfg.prompt_default = PROMPT_DEFAULT_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if not cfg.prompt_daily and PROMPT_DAILY_PATH.exists():
        try:
            cfg.prompt_daily = PROMPT_DAILY_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return cfg


def save_config(cfg: MailAgentConfig) -> None:
    """
    Atomically write MailAgentConfig to config.json and sync prompt files.
    """
    ensure_dirs()
    # Exclude runtime injected secrets from serialization
    data = cfg.model_dump()
    json_str = json.dumps(data, indent=2, ensure_ascii=False)

    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    try:
        tmp_path.write_text(json_str, encoding="utf-8")
        os.replace(tmp_path, CONFIG_PATH)
        # Keep a valid backup of the last successful write
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        try:
            PROJECT_CONFIG_PATH.write_text(json_str, encoding="utf-8")
        except Exception:
            pass
        # Sync prompt files to project root
        if cfg.prompt_default:
            try:
                PROMPT_DEFAULT_PATH.write_text(cfg.prompt_default, encoding="utf-8")
            except Exception:
                pass
        if cfg.prompt_daily:
            try:
                PROMPT_DAILY_PATH.write_text(cfg.prompt_daily, encoding="utf-8")
            except Exception:
                pass

        logger.debug(f"config.json saved ({len(json_str)} bytes).")
    except Exception as e:
        logger.error(f"Failed to save config.json: {e}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def get_browser_dir() -> Path:
    """Return the browser auth state directory."""
    ensure_dirs()
    return DATA_ROOT / "browser"


def get_state_db_path() -> Path:
    """Return the path for the SQLite sync state database."""
    ensure_dirs()
    return DATA_ROOT / "state" / "sync_store.db"


def get_log_path() -> Path:
    """Return the log file path."""
    ensure_dirs()
    log_dir = DATA_ROOT / "logs"
    return log_dir / "mailagent.log"


def get_secrets_path() -> Path:
    """Return the secrets file path."""
    ensure_dirs()
    return DATA_ROOT / "secrets" / "secrets.dat"
