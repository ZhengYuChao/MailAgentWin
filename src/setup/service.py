"""
Setup orchestration service.
Validates all Setup inputs, persists config, and starts workers.
Called by POST /api/setup/validate-and-start.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from loguru import logger

from src.setup.schema import MailAgentConfig
from src.setup import persistence
from src.setup.validators import (
    validate_notion_token,
    extract_notion_db_id,
    validate_notion_database,
    validate_email_syntax,
    find_outlook_account,
    NotionTokenError,
    InvalidNotionURLError,
    DatabaseAccessError,
    DatabaseSchemaError,
    InvalidEmailError,
    OutlookAccountNotFoundError,
    ClassicOutlookUnavailableError,
)
from src.security import dpapi_store


@dataclass
class SetupPayload:
    token: str
    email_template: str
    email: str
    calendar_template: str = ""


@dataclass
class SetupResult:
    ok: bool
    field_errors: dict = field(default_factory=dict)
    general_error: str = ""
    workers: list = field(default_factory=list)


async def validate_and_start(payload: SetupPayload) -> SetupResult:
    """
    Full Setup orchestration.
    """
    field_errors: dict[str, str] = {}

    # ── Step 1: Notion Token ──────────────────────────────────────────────────
    logger.info("Setup: Checking Notion Token…")
    try:
        await asyncio.to_thread(validate_notion_token, payload.token)
    except Exception as e:
        field_errors["token"] = str(e)

    if field_errors:
        return SetupResult(ok=False, field_errors=field_errors)

    # ── Step 2-3: Email Template ──────────────────────────────────────────────
    logger.info("Setup: Checking Email Template…")
    email_db_id = ""
    try:
        email_db_id = extract_notion_db_id(payload.email_template)
        await asyncio.to_thread(
            validate_notion_database, payload.token, email_db_id, "email"
        )
    except Exception as e:
        field_errors["emailTemplate"] = str(e)

    if field_errors:
        return SetupResult(ok=False, field_errors=field_errors)

    # ── Step 4-5: Email / Outlook account ─────────────────────────────────────
    logger.info("Setup: Checking Email…")
    try:
        validate_email_syntax(payload.email)
        await asyncio.to_thread(find_outlook_account, payload.email)
    except Exception as e:
        field_errors["email"] = str(e)

    if field_errors:
        return SetupResult(ok=False, field_errors=field_errors)

    # ── Step 6: Calendar Template (optional) ──────────────────────────────────
    calendar_db_id = ""
    calendar_enabled = False
    if payload.calendar_template and payload.calendar_template.strip():
        logger.info("Setup: Checking Calendar Template…")
        try:
            calendar_db_id = extract_notion_db_id(payload.calendar_template)
            await asyncio.to_thread(
                validate_notion_database, payload.token, calendar_db_id, "calendar"
            )
            calendar_enabled = True
        except Exception as e:
            field_errors["calendarTemplate"] = str(e)

    if field_errors:
        return SetupResult(ok=False, field_errors=field_errors)

    # ── Step 7: Encrypt token with DPAPI ──────────────────────────────────────
    logger.info("Setup: Starting workers…")
    try:
        await asyncio.to_thread(dpapi_store.encrypt_secret, "notion_token", payload.token)
    except Exception as e:
        logger.error(f"Failed to encrypt Notion token: {e}")
        return SetupResult(
            ok=False,
            general_error="Failed to securely store Notion Token. Check system permissions.",
        )

    # ── Steps 8-11: Build config, write atomically ────────────────────────────
    try:
        cfg = persistence.load_config()  # load any existing settings as base
        cfg.email_template_id = email_db_id
        cfg.email = payload.email
        cfg.calendar_template_id = calendar_db_id
        cfg.calendar_enabled = calendar_enabled
        cfg.setup_complete = False  # will set True after full write

        # Persist non-complete state first (token already encrypted)
        persistence.save_config(cfg)

        # Mark complete
        cfg.setup_complete = True
        persistence.save_config(cfg)
        logger.info("Setup: Configuration saved successfully.")

    except Exception as e:
        logger.error(f"Failed to persist configuration: {e}")
        return SetupResult(
            ok=False,
            general_error=f"MailAgent could not save configuration: {e}",
        )

    # ── Step 12: Start workers ────────────────────────────────────────────────
    try:
        from src.runtime.supervisor import get_supervisor
        supervisor = get_supervisor()
        supervisor.start_all()
    except Exception as e:
        logger.error(f"Worker startup failed after valid config: {e}")
        # Do NOT fail setup — navigate to Status with Abnormal state

    # Return current worker states
    try:
        from src.runtime.status_registry import registry
        workers = registry.snapshot_list()
    except Exception:
        workers = []

    return SetupResult(ok=True, workers=workers)
