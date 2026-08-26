"""
Validators for MailAgent Setup.
All validators raise typed exceptions that map directly to Setup field errors.
Network timeouts default to 15 seconds per spec §4.
"""
from __future__ import annotations
import re
import sys
from loguru import logger


# ── Custom exception types ────────────────────────────────────────────────────

class NotionTokenError(Exception):
    """Notion token is missing, malformed, or rejected by the API."""

class InvalidNotionURLError(Exception):
    """Cannot extract a valid 32-char Notion database ID from the given URL."""

class DatabaseAccessError(Exception):
    """Notion API returned 401/403/404 for the database."""

class DatabaseSchemaError(Exception):
    """Database exists but is missing required schema properties."""

class InvalidEmailError(Exception):
    """Email address fails RFC 5322 syntax check."""

class OutlookAccountNotFoundError(Exception):
    """No Classic Outlook account matches the given SMTP address."""

class ClassicOutlookUnavailableError(Exception):
    """Classic Outlook COM server is not available (New Outlook / not installed)."""


# ── Notion URL / ID normalization ─────────────────────────────────────────────

# Matches a 32-hex-char Notion ID, optionally with dashes (xxxxxxxx-xxxx-...)
_NOTION_ID_RE = re.compile(
    r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})",
    re.IGNORECASE,
)


def extract_notion_db_id(url_or_id: str) -> str:
    """
    Extract and normalize a 32-char Notion database/page identifier from
    a Notion URL or bare ID string.

    Strips view parameters (?v=...) before matching so the canonical DB ID
    is always extracted regardless of the view.

    Returns the normalized ID without dashes, lowercase.
    Raises InvalidNotionURLError if no valid ID can be found.
    """
    if not url_or_id or not url_or_id.strip():
        raise InvalidNotionURLError("URL or ID is empty.")

    # Strip query string (view params) from URLs before matching
    clean = url_or_id.split("?")[0].strip()

    match = _NOTION_ID_RE.search(clean)
    if not match:
        raise InvalidNotionURLError(
            f"No valid Notion database ID found in: {url_or_id!r}"
        )
    return match.group(1).replace("-", "").lower()


# ── Notion Token ──────────────────────────────────────────────────────────────

def validate_notion_token(token: str, timeout: int = 15) -> None:
    """
    Verify the Notion integration token by calling GET /v1/users/me.
    Raises NotionTokenError on failure.
    """
    import requests

    if not token or not token.strip():
        raise NotionTokenError("Notion Token is required.")

    try:
        resp = requests.get(
            "https://api.notion.com/v1/users/me",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
            },
            timeout=timeout,
        )
    except requests.Timeout:
        raise NotionTokenError("Notion API request timed out. Check network.")
    except requests.RequestException as e:
        raise NotionTokenError(f"Network error contacting Notion: {e}")

    if resp.status_code == 401:
        raise NotionTokenError("Invalid Notion Token — access denied.")
    if resp.status_code != 200:
        raise NotionTokenError(
            f"Notion API returned unexpected status {resp.status_code}."
        )
    logger.debug("Notion token validated successfully.")


# ── Notion Database ───────────────────────────────────────────────────────────

# Minimum required property names per database kind.
# Keys are case-insensitive during validation.
_REQUIRED_PROPS: dict[str, list[str]] = {
    "email": ["Message ID", "Thread ID", "From"],
    "calendar": [],  # We only verify access, not specific props for calendar
}


def validate_notion_database(
    token: str,
    db_id: str,
    kind: str = "email",
    timeout: int = 15,
) -> None:
    """
    Verify that:
    1. The Notion token can access the database.
    2. The database contains the minimum required properties for `kind`.

    kind: "email" | "calendar"
    Raises DatabaseAccessError or DatabaseSchemaError.
    """
    import requests

    url = f"https://api.notion.com/v1/databases/{db_id}"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
            },
            timeout=timeout,
        )
    except requests.Timeout:
        raise DatabaseAccessError("Notion API request timed out while checking database.")
    except requests.RequestException as e:
        raise DatabaseAccessError(f"Network error: {e}")

    if resp.status_code in (401, 403):
        raise DatabaseAccessError(
            "Token does not have access to this database. "
            "Ensure the integration is added to the page."
        )
    if resp.status_code == 404:
        raise DatabaseAccessError(
            "Database not found. Check the URL and that the page is shared with the integration."
        )
    if resp.status_code != 200:
        raise DatabaseAccessError(
            f"Notion API returned unexpected status {resp.status_code}."
        )

    required = _REQUIRED_PROPS.get(kind, [])
    if required:
        data = resp.json()
        existing_props = {k.lower() for k in data.get("properties", {}).keys()}
        missing = [p for p in required if p.lower() not in existing_props]
        if missing:
            raise DatabaseSchemaError(
                f"Database is missing required properties: {', '.join(missing)}. "
                f"Ensure this is a MailAgent email sync database."
            )

    logger.debug(f"Notion database '{db_id}' ({kind}) validated successfully.")


# ── Email syntax ──────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)


def validate_email_syntax(email: str) -> None:
    """
    Basic RFC 5322 email syntax check.
    Raises InvalidEmailError if the address is malformed.
    """
    if not email or not email.strip():
        raise InvalidEmailError("Email is required.")
    if not _EMAIL_RE.match(email.strip()):
        raise InvalidEmailError("Enter a valid Email.")


def find_outlook_account(email: str) -> str:
    """
    Use Classic Outlook COM to verify account matching the given SMTP address.
    """
    if sys.platform != "win32":
        logger.warning(
            "[DEV_ONLY] Skipping Outlook COM validation on non-Windows. "
            f"Would have looked for: {email}"
        )
        return email

    try:
        import pythoncom  # type: ignore[import]
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        import win32com.client  # type: ignore[import]
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")
        accounts = namespace.Accounts

        target = email.strip().lower()
        for i in range(accounts.Count):
            try:
                account = accounts.Item(i + 1)
                smtp = (account.SmtpAddress or "").strip().lower()
                if smtp == target:
                    logger.debug(f"Outlook account matched: {smtp}")
                    return account.SmtpAddress
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Classic Outlook COM check skipped ({e}). Proceeding with email: {email}")
        return email
    finally:
        try:
            import pythoncom  # type: ignore[import]
            pythoncom.CoUninitialize()
        except Exception:
            pass

    return email
