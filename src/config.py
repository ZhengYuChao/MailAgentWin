"""
MailAgent runtime configuration singleton.
Loads from config.json + Windows DPAPI secrets (not from .env).

All existing worker code using `from src.config import config` continues to work
unchanged — all attribute names from the original pydantic-settings Config class
are preserved as properties or direct fields on MailAgentConfig.
"""
from __future__ import annotations
import sys
import zoneinfo
from loguru import logger

from src.setup.schema import MailAgentConfig
from src.setup import persistence
from src.security import dpapi_store


def _load_config() -> MailAgentConfig:
    """Load config from config.json + DPAPI. Fall back to .env on Mac dev."""
    cfg = persistence.load_config()

    # Attach decrypted secrets
    for secret_name in ("notion_token", "llm_api_key", "feishu_app_secret", "feishu_webhook_secret", "alert_feishu_webhook_secret"):
        if dpapi_store.has_secret(secret_name):
            try:
                setattr(cfg, secret_name, dpapi_store.decrypt_secret(secret_name))
            except Exception as e:
                logger.error(f"Failed to decrypt secret '{secret_name}': {e}")
                setattr(cfg, secret_name, "")
        else:
            setattr(cfg, secret_name, "")

    return cfg


def get_system_local_tz() -> zoneinfo.ZoneInfo:
    """Auto-detect the host system local timezone (Windows, Mac, Linux)."""
    try:
        if sys.platform == 'win32':
            try:
                import tzlocal
                tz_name = str(tzlocal.get_localzone_name() if hasattr(tzlocal, 'get_localzone_name') else tzlocal.get_localzone())
                if tz_name:
                    return zoneinfo.ZoneInfo(tz_name)
            except Exception:
                pass
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                   r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
                    tz_key_name = winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
                _WIN_TO_IANA = {
                    "China Standard Time": "Asia/Shanghai",
                    "Taipei Standard Time": "Asia/Taipei",
                    "Tokyo Standard Time": "Asia/Tokyo",
                    "Korea Standard Time": "Asia/Seoul",
                    "Pacific Standard Time": "America/Los_Angeles",
                    "Eastern Standard Time": "America/New_York",
                    "Central Standard Time": "America/Chicago",
                    "Mountain Standard Time": "America/Denver",
                    "GMT Standard Time": "Europe/London",
                    "W. Europe Standard Time": "Europe/Berlin",
                    "Romance Standard Time": "Europe/Paris",
                    "Singapore Standard Time": "Asia/Singapore",
                    "AUS Eastern Standard Time": "Australia/Sydney",
                    "India Standard Time": "Asia/Kolkata",
                    "CST": "Asia/Shanghai",
                }
                iana_name = _WIN_TO_IANA.get(tz_key_name)
                if iana_name:
                    return zoneinfo.ZoneInfo(iana_name)
            except Exception:
                pass

        # Try tzlocal generally
        try:
            import tzlocal
            tz_obj = tzlocal.get_localzone()
            tz_str = str(tz_obj)
            if tz_str:
                return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            pass

        # Native Python fallback via datetime
        from datetime import datetime
        local_tz = datetime.now().astimezone().tzinfo
        if hasattr(local_tz, 'key') and local_tz.key:
            return zoneinfo.ZoneInfo(local_tz.key)
        return local_tz or zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception as e:
        logger.warning(f"Failed to auto-detect system timezone: {e}. Defaulting to Asia/Shanghai")
        return zoneinfo.ZoneInfo("Asia/Shanghai")


class _ConfigProxy:
    """
    Lazy-loading config proxy that mirrors the original src/config.py interface.
    Workers use `from src.config import config` and access attributes directly.
    The proxy auto-reloads when config.json is modified on disk across processes.
    """

    def __init__(self) -> None:
        self._cfg: MailAgentConfig | None = None
        self._last_mtime: float = 0.0

    def _ensure(self) -> MailAgentConfig:
        try:
            config_path = persistence.get_config_path()
            current_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
        except Exception:
            current_mtime = 0.0

        if self._cfg is None or (current_mtime > 0 and current_mtime != self._last_mtime):
            self._cfg = _load_config()
            self._last_mtime = current_mtime
        return self._cfg

    def reload(self) -> None:
        """Reload configuration from disk. Called after Settings save."""
        self._cfg = _load_config()
        try:
            config_path = persistence.get_config_path()
            self._last_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
        except Exception:
            self._last_mtime = 0.0
        logger.info("Configuration reloaded from config.json.")

    # ── Attribute access — delegates to MailAgentConfig ─────────────────────

    def __getattr__(self, name: str):
        cfg = self._ensure()
        # Direct fields
        if name in type(cfg).model_fields:
            return getattr(cfg, name)
        # Properties (aliases)
        if hasattr(cfg, name):
            return getattr(cfg, name)
        # Case-insensitive lookup (e.g. NOTION_AI_PAGE_URL -> notion_ai_page_url)
        lower_name = name.lower()
        if lower_name in type(cfg).model_fields:
            return getattr(cfg, lower_name)
        if hasattr(cfg, lower_name):
            return getattr(cfg, lower_name)
        if lower_name == "notion_ai_page_url":
            return "https://app.notion.com/ai"
        logger.warning(f"Config attribute '{name}' not found, returning empty string fallback.")
        return ""

    # ── Timezone helper (matches original config.tz property) ────────────────

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        cfg = self._ensure()
        tz_str = (cfg.app_timezone or "").strip()
        if not tz_str or tz_str.lower() in ("auto", "default", "system"):
            return get_system_local_tz()
        try:
            return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            return get_system_local_tz()


config = _ConfigProxy()

