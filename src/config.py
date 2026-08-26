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
        if name in cfg.model_fields:
            return getattr(cfg, name)
        # Properties (aliases)
        if hasattr(cfg, name):
            return getattr(cfg, name)
        # Injected secrets or runtime attributes
        if hasattr(cfg, name):
            return getattr(cfg, name)
        logger.warning(f"Config attribute '{name}' not found, returning empty string fallback.")
        return ""

    # ── Timezone helper (matches original config.tz property) ────────────────

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        cfg = self._ensure()
        return zoneinfo.ZoneInfo(cfg.app_timezone)


config = _ConfigProxy()
