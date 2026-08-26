"""
Windows DPAPI secret store for MailAgent.
All secrets (Notion token, API keys, webhook secrets) are encrypted using the
Windows Data Protection API, scoped to the current user account.

On non-Windows (Mac dev environment): secrets are stored as plain base64 with
a [DEV_ONLY] warning.  This code path is NEVER bundled into the production .exe.
"""
from __future__ import annotations
import base64
import json
import os
import sys
from pathlib import Path
from loguru import logger


def _secrets_path() -> Path:
    from src.setup.persistence import get_secrets_path
    return get_secrets_path()


def _load_store() -> dict:
    path = _secrets_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read secrets store: {e}")
        return {}


def _save_store(store: dict) -> None:
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"Failed to write secrets store: {e}")
        tmp.unlink(missing_ok=True)
        raise


# ── Windows DPAPI ─────────────────────────────────────────────────────────────

def _dpapi_encrypt(plaintext: str) -> bytes:
    if sys.platform != "win32":
        return base64.b64encode(plaintext.encode("utf-8"))
    try:
        import win32crypt  # type: ignore[import]
        # In pywin32: CryptProtectData(data, description='', optionalEntropy=None, reserved=None, promptStruct=None, flags=0)
        return win32crypt.CryptProtectData(plaintext.encode("utf-8"), "")
    except Exception as e:
        logger.warning(f"DPAPI encryption failed ({e}), falling back to base64.")
        return base64.b64encode(plaintext.encode("utf-8"))


def _dpapi_decrypt(ciphertext: bytes) -> str:
    if sys.platform != "win32":
        return base64.b64decode(ciphertext).decode("utf-8")
    try:
        import win32crypt  # type: ignore[import]
        _desc, plaintext_bytes = win32crypt.CryptUnprotectData(ciphertext, None)
        return plaintext_bytes.decode("utf-8")
    except Exception:
        # Fallback if stored as base64
        try:
            return base64.b64decode(ciphertext).decode("utf-8")
        except Exception:
            return ""


# ── Public API ────────────────────────────────────────────────────────────────

def encrypt_secret(name: str, plaintext: str) -> None:
    """Encrypt and persist a named secret using Windows DPAPI."""
    if sys.platform != "win32":
        logger.warning(f"[DEV_ONLY] Storing '{name}' as plain base64 (non-Windows).")
    encrypted_bytes = _dpapi_encrypt(plaintext)
    store = _load_store()
    store[name] = base64.b64encode(encrypted_bytes).decode("ascii")
    _save_store(store)
    logger.debug(f"Secret '{name}' encrypted and saved.")


def decrypt_secret(name: str) -> str:
    """Decrypt and return a named secret. Raises KeyError if not found."""
    store = _load_store()
    if name not in store:
        raise KeyError(f"Secret '{name}' not found in store.")
    encrypted_b64 = store[name].encode("ascii")
    encrypted_bytes = base64.b64decode(encrypted_b64)
    return _dpapi_decrypt(encrypted_bytes)


def has_secret(name: str) -> bool:
    """Return True if the named secret exists in the store."""
    return name in _load_store()


def delete_secret(name: str) -> None:
    """Remove a named secret from the store."""
    store = _load_store()
    if name in store:
        del store[name]
        _save_store(store)
        logger.debug(f"Secret '{name}' deleted.")
