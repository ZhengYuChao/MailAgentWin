"""
Local aiohttp UI server for MailAgent.
Binds to 127.0.0.1 on a random port.
Serves static files and all /api/* routes.
Security: origin check + CSRF header on mutating routes.
"""
from __future__ import annotations
import asyncio
import json
import os
import secrets
import threading
import time
from pathlib import Path
from loguru import logger

from aiohttp import web

# ── Path to static files (supports both source and PyInstaller frozen exe) ─────
def _get_static_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass_static = Path(sys._MEIPASS) / "src" / "ui" / "static"
        if meipass_static.exists():
            return meipass_static
    return Path(__file__).parent / "static"

STATIC_DIR = _get_static_dir()

# ── CSRF token (one per server lifetime) ─────────────────────────────────────
CSRF_TOKEN = secrets.token_hex(32)

# ── Notion auth session state ─────────────────────────────────────────────────
_notion_auth_state: dict = {
    "status": "idle",   # "idle" | "waiting" | "complete" | "error"
    "reason": "",
    "browser_task": None,
}
_notion_auth_lock = asyncio.Lock()


# ── Middleware ────────────────────────────────────────────────────────────────

@web.middleware
async def security_middleware(request: web.Request, handler):
    """Enforce origin check for local security."""
    origin = request.headers.get("Origin", "")

    # Allow localhost / 127.0.0.1 or direct requests
    if origin and not (
        origin.startswith("http://127.0.0.1") or
        origin.startswith("http://localhost")
    ):
        raise web.HTTPForbidden(reason="Invalid origin")

    # If CSRF token is sent, verify it
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.path.startswith("/api/"):
        token = request.headers.get("X-CSRF-Token", "")
        if token and token != CSRF_TOKEN:
            raise web.HTTPForbidden(reason="Invalid CSRF token")

    return await handler(request)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ok(data: dict = None) -> web.Response:
    return web.Response(
        content_type="application/json",
        text=json.dumps({"ok": True, "data": data or {}}, ensure_ascii=False),
    )


def _err(code: str, message: str, field_errors: dict = None,
         retryable: bool = False, status: int = 400) -> web.Response:
    error = {"code": code, "message": message, "retryable": retryable}
    if field_errors:
        error["fieldErrors"] = field_errors
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps({"ok": False, "error": error}, ensure_ascii=False),
    )


async def _read_json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")


# ── Static files & CSRF ───────────────────────────────────────────────────────

async def handle_csrf(request: web.Request) -> web.Response:
    return _ok({"csrfToken": CSRF_TOKEN})


async def handle_index(request: web.Request) -> web.Response:
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return web.Response(
            text=index_path.read_text(encoding="utf-8"),
            content_type="text/html",
            headers={"X-CSRF-Token": CSRF_TOKEN, "Access-Control-Expose-Headers": "X-CSRF-Token"},
        )
    return web.Response(text="MailAgent UI not found.", status=404)


# ── Setup ─────────────────────────────────────────────────────────────────────

async def handle_setup_validate(request: web.Request) -> web.Response:
    body = await _read_json(request)
    token = (body.get("token") or "").strip()
    email_template = (body.get("emailTemplate") or "").strip()
    email = (body.get("email") or "").strip()
    calendar_template = (body.get("calendarTemplate") or "").strip()

    from src.setup.persistence import load_config
    from src.security import dpapi_store

    cfg = load_config()
    if not token and dpapi_store.has_secret("notion_token"):
        token = dpapi_store.decrypt_secret("notion_token")
    if not email_template and cfg.email_template_id:
        email_template = cfg.email_template_id
    if not email and cfg.email:
        email = cfg.email
    if not calendar_template and cfg.calendar_template_id:
        calendar_template = cfg.calendar_template_id

    from src.setup.service import SetupPayload, validate_and_start
    payload = SetupPayload(
        token=token,
        email_template=email_template,
        email=email,
        calendar_template=calendar_template,
    )

    try:
        result = await asyncio.wait_for(validate_and_start(payload), timeout=90)
    except asyncio.TimeoutError:
        return _err("timeout", "MailAgent could not complete setup. Try again.", retryable=True, status=504)
    except Exception as e:
        logger.error(f"Setup error: {e}")
        return _err("internal", str(e), retryable=True, status=500)

    if not result.ok:
        return _err(
            "validation_failed",
            result.general_error or "Please correct the fields below.",
            field_errors=result.field_errors,
            status=422,
        )

    return _ok({"setupComplete": True, "workers": result.workers})


# ── Notion AI Auth ────────────────────────────────────────────────────────────

_playwright_instance = None
_playwright_browser = None
_playwright_context = None


async def _cleanup_auth_browser():
    global _playwright_instance, _playwright_browser, _playwright_context
    try:
        if _playwright_context:
            await _playwright_context.close()
    except Exception:
        pass
    try:
        if _playwright_browser:
            await _playwright_browser.close()
    except Exception:
        pass
    try:
        if _playwright_instance:
            await _playwright_instance.stop()
    except Exception:
        pass
    _playwright_context = None
    _playwright_browser = None
    _playwright_instance = None


async def handle_notion_auth_start(request: web.Request) -> web.Response:
    """Launch visible Playwright browser for Notion login and AI model selection."""
    global _playwright_instance, _playwright_browser, _playwright_context
    from playwright.async_api import async_playwright
    from src.setup.persistence import get_browser_dir, load_config

    user_email = ""
    try:
        if request.can_read_body:
            body = await request.json()
            user_email = (body.get("email") or "").strip()
    except Exception:
        pass

    await _cleanup_auth_browser()

    auth_dir = get_browser_dir()
    auth_dir.mkdir(parents=True, exist_ok=True)
    ua_path = auth_dir / "user_agent.txt"
    root_ua = Path(__file__).parent.parent.parent / "user_agent.txt"

    try:
        _playwright_instance = await async_playwright().start()
        _playwright_browser = await _playwright_instance.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        _playwright_context = await _playwright_browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai"
        )
        page = await _playwright_context.new_page()

        # Capture User-Agent
        user_agent = await page.evaluate("navigator.userAgent")
        ua_path.write_text(user_agent, encoding="utf-8")
        try:
            root_ua.write_text(user_agent, encoding="utf-8")
        except Exception:
            pass

        cfg = load_config()
        target_url = cfg.notion_ai_page_url or "https://app.notion.com/ai"

        async def _open_and_prefill():
            try:
                if user_email:
                    logger.info(f"🌐 Opening Notion login with prefilled email: {user_email}")
                    await page.goto("https://www.notion.so/login", timeout=60000)
                    for sel in ['input[type="email"]', '#notion-email-input-1', 'input[name="email"]', 'input[placeholder*="email" i]']:
                        try:
                            el = await page.wait_for_selector(sel, timeout=4000)
                            if el:
                                await page.fill(sel, user_email)
                                logger.info(f"✅ Email prefilled into selector: {sel}")
                                break
                        except Exception:
                            continue
                else:
                    logger.info(f"🌐 Opening Notion page at: {target_url}")
                    await page.goto(target_url, timeout=60000)
            except Exception as ex:
                logger.debug(f"Navigation / prefill: {ex}")

        asyncio.create_task(_open_and_prefill())

        return _ok({"status": "browser_opened", "message": "Browser opened. Complete login and select model, then click Continue."})
    except Exception as e:
        logger.error(f"Failed to launch Notion login browser: {e}")
        await _cleanup_auth_browser()
        return _err("browser_launch_failed", f"Failed to launch browser: {e}", status=500)


async def handle_notion_auth_continue(request: web.Request) -> web.Response:
    """Save auth state from the open browser context and close it."""
    global _playwright_context
    from src.setup.persistence import get_browser_dir, load_config, save_config

    auth_dir = get_browser_dir()
    auth_path = auth_dir / "notion_auth.json"
    root_auth = Path(__file__).parent.parent.parent / "notion_auth.json"

    if _playwright_context is None:
        # Check if already present on disk
        if auth_path.exists() or root_auth.exists():
            cfg = load_config()
            cfg.notion_auth_complete = True
            save_config(cfg)
            return _ok({"status": "complete", "message": "Using existing Notion authentication."})
        return _err("no_session", "No active browser session found. Please click 'Sign in to Notion' first.", status=400)

    try:
        # Extract available Notion AI models from the open page by actively checking the model selector
        extracted_models = []
        try:
            for p in _playwright_context.pages:
                if "notion.so" in p.url or "notion.com" in p.url:
                    # 1. Try to focus chat input
                    try:
                        inp = p.locator("div[contenteditable='true'], [role='textbox']").locator("visible=true").last
                        if await inp.count() > 0:
                            await inp.click(delay=50, timeout=1000)
                    except Exception:
                        pass

                    # 2. Click model selector button
                    try:
                        btn = p.locator('[data-testid="unified-chat-model-button"]').first
                        if await btn.count() == 0:
                            btn = p.locator("div[role='button'][aria-haspopup='dialog']").first
                        if await btn.count() > 0:
                            await btn.click(force=True, delay=100)
                            await asyncio.sleep(1.0)
                            
                            # Expand older models if present
                            try:
                                older_btn = p.locator('.notion-overlay-container [role="menuitem"], .notion-overlay-container [role="button"]').filter(has_text='Older models').first
                                if await older_btn.count() > 0 and await older_btn.is_visible(timeout=1000):
                                    await older_btn.click(delay=100)
                                    await asyncio.sleep(1.0)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # 3. Extract models from open overlay / menu items
                    raw_items = await p.evaluate('''() => {
                        const overlay = document.querySelector('.notion-overlay-container');
                        const items = overlay ? overlay.querySelectorAll('[role="menuitem"], [role="option"], [role="menuitemradio"]') : [];
                        const results = [];
                        items.forEach(el => {
                            const text = el.innerText || '';
                            const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                            if (lines.length > 0) {
                                results.push(lines);
                            }
                        });
                        return results;
                    }''')

                    models = []
                    for lines in raw_items:
                        if not lines:
                            continue
                        first = lines[0]
                        if first.lower() == "older models":
                            continue
                        if first.lower() == "auto":
                            if "Auto" not in models:
                                models.append("Auto")
                            continue
                        if len(lines) >= 2:
                            second = lines[1]
                            if len(second) > 25 or any(second.startswith(w) for w in ["Balances", "Best for", "Fastest", "Most powerful", "Hosted by", "Advanced", "Designed for"]):
                                name = first
                            else:
                                name = f"{first} {second}"
                        else:
                            name = first
                        if name and name not in models:
                            models.append(name)

                    # Press Escape to close any opened dropdown
                    try:
                        await p.keyboard.press("Escape")
                    except Exception:
                        pass

                    if models:
                        extracted_models = models
                        break
        except Exception as ex:
            logger.debug(f"Model extraction: {ex}")

        await _playwright_context.storage_state(path=str(auth_path))
        try:
            await _playwright_context.storage_state(path=str(root_auth))
        except Exception:
            pass

        logger.info(f"✅ Notion auth state saved successfully to {auth_path}")
        await _cleanup_auth_browser()

        cfg = load_config()
        cfg.notion_auth_complete = True
        if extracted_models:
            cfg.available_ai_models = extracted_models
            logger.info(f"🤖 Extracted available Notion AI models: {cfg.available_ai_models}")
        save_config(cfg)

        return _ok({"status": "complete", "message": "Notion AI signed in successfully.", "available_ai_models": cfg.available_ai_models})
    except Exception as e:
        logger.error(f"Failed to save Notion auth: {e}")
        await _cleanup_auth_browser()
        return _err("auth_save_failed", f"Failed to save login state: {e}", status=500)


async def handle_ai_sync_models(request: web.Request) -> web.Response:
    """Manually trigger discovery and sync of Notion AI models via headless browser."""
    from src.setup.persistence import load_config
    try:
        from src.ai.controller import AIController
        controller = AIController()
        success = await controller._ensure_browser()
        if success:
            models = await controller.sync_available_models()
            await controller.close()
            cfg = load_config()
            return _ok({"available_ai_models": cfg.available_ai_models})
        else:
            await controller.close()
            return _err("browser_init_failed", "Failed to launch headless browser for model sync", status=500)
    except Exception as e:
        logger.error(f"Failed to sync models on demand: {e}")
        return _err("sync_failed", str(e), status=500)


async def handle_notion_auth_status(request: web.Request) -> web.Response:
    """Check current Notion auth status."""
    global _playwright_context
    from src.setup.persistence import get_browser_dir, load_config
    auth_path = get_browser_dir() / "notion_auth.json"
    root_auth = Path(__file__).parent.parent.parent / "notion_auth.json"

    is_complete = False
    if (auth_path.exists() and auth_path.stat().st_size > 50) or (root_auth.exists() and root_auth.stat().st_size > 50):
        is_complete = True

    if _playwright_context is not None:
        return _ok({"status": "browser_opened", "is_complete": is_complete})

    if is_complete:
        return _ok({"status": "complete", "is_complete": True})

    return _ok({"status": "idle", "is_complete": False})


# ── Runtime Status ────────────────────────────────────────────────────────────

async def handle_runtime_status(request: web.Request) -> web.Response:
    from src.runtime.status_registry import registry
    return _ok(registry.snapshot())


async def handle_worker_restart(request: web.Request) -> web.Response:
    worker_id = request.match_info.get("workerId", "")
    from src.runtime.supervisor import get_supervisor
    result = get_supervisor().restart_worker(worker_id)
    if result["ok"]:
        return _ok({"message": result["message"]})
    return _err("restart_failed", result["message"], status=400)


async def handle_runtime_restart(request: web.Request) -> web.Response:
    from src.runtime.supervisor import get_supervisor
    get_supervisor().stop_all()
    await asyncio.sleep(1)
    get_supervisor().start_all()
    return _ok({"message": "MailAgent restarting."})


async def handle_force_sync(request: web.Request) -> web.Response:
    from src.runtime.supervisor import get_supervisor
    from src.setup.persistence import load_config
    cfg = load_config()
    days = getattr(cfg, "force_sync_days", 3)
    get_supervisor().trigger_force_sync(days)
    return _ok({"message": f"Force sync initiated for the last {days} days."})


async def handle_connectivity(request: web.Request) -> web.Response:
    """Check internet and Notion API connectivity."""
    import aiohttp
    checks = []
    
    async def _check(name: str, urls: list[str], timeout_sec: float = 4.0):
        result = {"name": name, "ok": False, "latency_ms": None, "error": None}
        for url in urls:
            try:
                start = time.time()
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_sec),
                                           ssl=False) as resp:
                        # Any HTTP status response (including 401/403/404) means network reachability
                        result["ok"] = True
                        result["latency_ms"] = int((time.time() - start) * 1000)
                        result["error"] = None
                        break
            except asyncio.TimeoutError:
                result["error"] = "Connection timed out"
            except Exception as e:
                result["error"] = str(e)[:100]
        checks.append(result)
    
    await asyncio.gather(
        _check("Internet Connection", ["https://www.msftconnecttest.com/connecttest.txt", "https://www.baidu.com", "https://www.qq.com"]),
        _check("Notion Service", ["https://api.notion.com/v1/users/me", "https://www.notion.so"]),
    )
    return _ok({"checks": checks})

# ── Settings ──────────────────────────────────────────────────────────────────

async def handle_settings_get(request: web.Request) -> web.Response:
    from src.setup.persistence import load_config
    from src.security.dpapi_store import has_secret
    cfg = load_config()
    data = cfg.model_dump()
    # Never return raw token — return presence flag only
    data["notionTokenPresent"] = has_secret("notion_token")
    data.pop("notion_token", None)
    data.pop("llm_api_key", None)
    return _ok(data)


async def handle_settings_put(request: web.Request) -> web.Response:
    body = await _read_json(request)
    from src.setup.persistence import load_config, save_config
    from src.setup.schema import MailAgentConfig
    from src.security import dpapi_store
    from src.runtime.supervisor import get_supervisor

    cfg = load_config()

    # Handle token replacement separately
    new_token = body.pop("notionToken", None)
    if new_token and new_token.strip():
        from src.setup.validators import validate_notion_token, NotionTokenError
        try:
            await asyncio.to_thread(validate_notion_token, new_token.strip())
            dpapi_store.encrypt_secret("notion_token", new_token.strip())
        except NotionTokenError as e:
            return _err("validation_failed", str(e),
                       field_errors={"notionToken": str(e)}, status=422)

    # Handle LLM key replacement
    new_llm_key = body.pop("llmApiKey", None)
    if new_llm_key and new_llm_key.strip():
        dpapi_store.encrypt_secret("llm_api_key", new_llm_key.strip())

    # Update all other fields
    current_data = cfg.model_dump()
    for k, v in body.items():
        if k in current_data:
            current_data[k] = v

    try:
        new_cfg = MailAgentConfig(**current_data)
        save_config(new_cfg)
    except Exception as e:
        return _err("save_failed", f"Failed to save settings: {e}", status=500)

    # Reload config singleton
    from src.config import config as cfg_proxy
    cfg_proxy.reload()

    # Automatically restart workers if supervisor is active to apply new configuration
    try:
        from src.runtime.supervisor import get_supervisor
        sup = get_supervisor()
        if sup._proc_mail and sup._proc_mail.is_alive():
            logger.info("Settings updated: restarting MailWorker to apply new configuration...")
            sup.restart_worker("mail")
        if sup._proc_ai and sup._proc_ai.is_alive():
            logger.info("Settings updated: restarting AIWorker to apply new configuration...")
            sup.restart_worker("ai")
    except Exception as e:
        logger.warning(f"Failed to auto-restart workers on settings update: {e}")

    return _ok({"saved": True})


# ── Logs ─────────────────────────────────────────────────────────────────────

async def handle_logs_get(request: web.Request) -> web.Response:
    cursor = int(request.rel_url.query.get("cursor", "-1"))
    worker_filter = request.rel_url.query.get("worker", "")
    search = request.rel_url.query.get("search", "")
    from src.runtime.log_stream import read_lines
    lines, new_cursor = read_lines(cursor=cursor, max_lines=250,
                                   worker_filter=worker_filter, search=search)
    return _ok({"lines": lines, "cursor": new_cursor})


async def handle_logs_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint for live log streaming."""
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    from src.runtime.log_stream import tail_new_lines
    cursor = 0
    try:
        while True:
            lines, cursor = tail_new_lines(cursor)
            if lines:
                data = json.dumps(lines, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
            await asyncio.sleep(1)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application(middlewares=[security_middleware])

    # Static files & CSRF
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/csrf", handle_csrf)
    app.router.add_static("/static", STATIC_DIR, show_index=False)

    # Setup
    app.router.add_post("/api/setup/validate-and-start", handle_setup_validate)

    # Notion auth
    app.router.add_post("/api/auth/notion-ai/start", handle_notion_auth_start)
    app.router.add_post("/api/auth/notion-ai/continue", handle_notion_auth_continue)
    app.router.add_get("/api/auth/notion-ai/status", handle_notion_auth_status)

    # Runtime
    app.router.add_get("/api/runtime/status", handle_runtime_status)
    app.router.add_post("/api/runtime/workers/{workerId}/restart", handle_worker_restart)
    app.router.add_post("/api/runtime/restart", handle_runtime_restart)
    app.router.add_post("/api/runtime/force_sync", handle_force_sync)
    app.router.add_get("/api/runtime/connectivity", handle_connectivity)

    # Settings
    app.router.add_get("/api/settings", handle_settings_get)
    app.router.add_put("/api/settings", handle_settings_put)
    app.router.add_post("/api/ai/sync-models", handle_ai_sync_models)

    # Logs
    app.router.add_get("/api/logs", handle_logs_get)
    app.router.add_get("/api/logs/stream", handle_logs_stream)

    return app


def run_server(port: int) -> None:
    """Run the aiohttp server in a dedicated thread's event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = create_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", port)
    loop.run_until_complete(site.start())
    logger.info(f"MailAgent UI server started at http://127.0.0.1:{port}")
    loop.run_forever()
