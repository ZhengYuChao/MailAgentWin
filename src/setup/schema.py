"""
MailAgent configuration schema.
Single source of truth for all settings — stored in config.json.
The Notion token is NOT stored here; it lives in secrets.dat (DPAPI-encrypted).
"""
from __future__ import annotations
from pydantic import BaseModel, field_validator
from typing import Optional


class MailAgentConfig(BaseModel):
    """All MailAgent configuration.  Non-secret values only — token lives in DPAPI store."""

    # ── Setup completion flags ──────────────────────────────────────────────
    setup_complete: bool = False
    notion_auth_complete: bool = False  # notion_auth.json saved + verified
    theme: str = "light"                # "light" | "dark"

    # ── Connection (Setup fields) ───────────────────────────────────────────
    # Normalized 32-char hex Notion database ID (no dashes, no view params)
    email_template_id: str = ""
    email: str = ""                     # SMTP address for Outlook account match
    calendar_template_id: str = ""      # empty → calendar disabled
    calendar_enabled: bool = False
    force_sync_days: int = 3            # Default days to sync on Force Sync

    # ── AI ──────────────────────────────────────────────────────────────────
    ai_mode: str = "notion_ai"
    ai_enabled: bool = True
    notion_ai_page_url: str = "https://app.notion.com/ai"
    notion_ai_batch_size: int = 2
    notion_ai_max_chats_per_session: int = 5
    notion_ai_max_new_chats_before_browser_restart: int = 8
    debounce_quiet_sec: int = 30
    debounce_force_sec: int = 1800
    notion_ai_fallback_wait_sec: int = 120
    notion_ai_wait_timeout: int = 600
    notion_ai_trigger_historical: bool = False

    # AI model selection (dynamically discovered by headless browser on startup)
    ai_model_email_sync: str = "Auto"
    ai_model_daily_summary: str = "Auto"
    available_ai_models: list[str] = ["Auto"]

    # ── Prompts ─────────────────────────────────────────────────────────────
    prompt_default: str = ""
    prompt_daily: str = ""

    # Direct LLM mode (optional, default disabled)
    llm_agent_enabled: bool = False
    llm_api_base: str = ""
    # llm_api_key stored in DPAPI under "llm_api_key" — empty here means absent
    llm_model: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 4096
    llm_timeout_sec: int = 60
    llm_context_page_id: str = ""
    llm_context_cache_ttl_sec: int = 1800
    llm_max_retries: int = 3
    llm_body_max_chars: int = 12000
    llm_cache_enabled: bool = True
    llm_cache_ttl: str = "1h"
    llm_inbox_prompt_path: str = "prompts/email_inbox.md"
    llm_sent_prompt_path: str = "prompts/email_sent.md"
    llm_daily_digest_database_id: str = ""
    llm_daily_digest_report_date_prop: str = "Report Date"

    # ── Sync ────────────────────────────────────────────────────────────────
    sync_mode: str = "hybrid"          # hybrid / applescript_only
    sync_initial_lookback_days: int = 7
    sync_mail_batch_size: int = 50
    sync_inbox_enabled: bool = True
    sync_sent_enabled: bool = True
    radar_poll_interval: int = 5
    reverse_sync_interval: int = 30
    sync_date_mode: str = "relative"   # relative / fixed
    sync_start_date: str = "2026-01-01"
    sync_lookback_days: int = 14
    startup_lookback_days: int = 7
    health_check_interval: int = 3600
    init_batch_size: int = 100
    sync_mailboxes: str = "Inbox"

    # ── Startup / Workers ───────────────────────────────────────────────────
    startup_enabled: bool = True
    startup_delay_seconds: int = 60
    workers_auto_restart: bool = True

    # ── Calendar ─────────────────────────────────────────────────────────── 
    calendar_refresh_seconds: int = 120
    calendar_past_days: int = 7
    calendar_future_days: int = 90
    calendar_sync_mode: str = "applescript"
    calendar_name: str = "日历"

    # ── Mail / Outlook ──────────────────────────────────────────────────────
    # Default matches English Outlook (Windows): Inbox / Sent Items
    mail_account_name: str = "Exchange"
    mail_account_url_prefix: str = "ews://"
    mail_inbox_name: str = "Inbox"
    mail_sent_name: str = "Sent Items"
    app_timezone: str = "auto"
    applescript_timeout: int = 200
    outlook_publish_timeout_sec: int = 600
    max_attachment_size: int = 20971520  # 20 MB

    # ── Optional — Office ───────────────────────────────────────────────────
    office_convert_enabled: bool = True

    # ── Optional — Keep-alive ───────────────────────────────────────────────
    keep_alive_enabled: bool = False
    keep_alive_dim: bool = True

    # ── Optional — Feishu notifications (sensitive → default empty/disabled) ─
    feishu_notify_enabled: bool = False
    feishu_app_id: str = ""
    # feishu_app_secret stored in DPAPI under "feishu_app_secret"
    feishu_chat_id: str = ""
    feishu_webhook_url: str = ""
    # feishu_webhook_secret stored in DPAPI under "feishu_webhook_secret"

    # ── Optional — Feishu error alerts ──────────────────────────────────────
    alert_enabled: bool = False
    alert_feishu_webhook_url: str = ""
    # alert_feishu_webhook_secret stored in DPAPI under "alert_feishu_webhook_secret"
    alert_levels: str = "critical,error,warning"
    alert_cooldown: int = 300
    alert_dead_letter_threshold: int = 5

    # ── Optional — Reverse proxy / Webhook server ────────────────────────── 
    reverse_proxy: str = ""            # ngrok / cloudflare / empty
    cloudflare_tunnel_token: str = ""  # Cloudflare Zero Trust tunnel token (for permanent fixed domain)
    cloudflare_custom_hostname: str = "" # Custom fixed hostname mapped to tunnel (e.g. mail.yourdomain.com)
    ngrok_custom_domain: str = ""      # ngrok static domain (e.g. name.ngrok-free.app)
    new_mail_database_id: str = ""

    # ── Optional — Redis event consumer ─────────────────────────────────────
    redis_events_enabled: bool = False
    redis_url: str = ""
    redis_db: int = 2

    # ── Optional — Stats reporting ───────────────────────────────────────────
    stats_report_url: str = ""
    # stats_report_token stored in DPAPI under "stats_report_token"
    stats_report_interval: int = 60

    # ── Optional — Misc ──────────────────────────────────────────────────────
    reverse_actions_enabled: bool = False
    notifications_enabled: bool = False
    error_alerts_enabled: bool = False
    logging_level: str = "INFO"

    # ── Project progress sync ────────────────────────────────────────────────
    project_progress_sync_enabled: bool = False
    project_progress_auto_sync_enabled: bool = False
    project_progress_sender: str = ""
    project_progress_subject_pattern: str = ""
    project_progress_database_id: str = ""
    project_progress_filter_bu: str = "TPS-ENBU"

    # ── Runtime-injected secrets (NOT serialized to config.json) ─────────────
    # These are populated by src/config.py after DPAPI decryption.
    # model_dump() excludes them so they never appear in config.json.
    notion_token: str = ""
    llm_api_key: str = ""
    feishu_app_secret: str = ""
    feishu_webhook_secret: str = ""
    alert_feishu_webhook_secret: str = ""

    def model_dump(self, **kwargs):
        """Override to exclude runtime-injected secrets from serialization."""
        exclude = kwargs.pop("exclude", set()) or set()
        exclude |= {
            "notion_token",
            "llm_api_key",
            "feishu_app_secret",
            "feishu_webhook_secret",
            "alert_feishu_webhook_secret",
        }
        return super().model_dump(exclude=exclude, **kwargs)

    @field_validator("email_template_id", "calendar_template_id", "new_mail_database_id", mode="before")
    @classmethod
    def normalize_notion_id(cls, v: str) -> str:
        """Strip dashes from Notion IDs stored in config."""
        if v:
            return v.replace("-", "").lower()
        return v

    # ── Convenience properties matching old src/config.py attribute names ───
    # These allow all existing worker code to keep using config.email_database_id etc.

    @property
    def email_database_id(self) -> str:
        return self.email_template_id

    @property
    def user_email(self) -> str:
        return self.email

    @property
    def calendar_database_id(self) -> str:
        return self.calendar_template_id

    @property
    def log_level(self) -> str:
        return self.logging_level

    @property
    def log_file(self) -> str:
        return "logs/mailagent.log"

    @property
    def calendar_check_interval(self) -> int:
        return self.calendar_refresh_seconds

    @property
    def notion_ai_batch_size_compat(self) -> int:
        return self.notion_ai_batch_size

    class Config:
        # Allow extra fields gracefully (forward compatibility)
        extra = "ignore"
