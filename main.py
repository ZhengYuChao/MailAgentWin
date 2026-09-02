"""
MailAgent entry point.
1. Ensures data directories exist.
2. Starts the local aiohttp UI server on a random port.
3. Opens the browser at http://127.0.0.1:{port}.
4. If setup is complete, starts workers via Supervisor.
   If not complete, Supervisor waits for setupComplete=True.
"""
import sys
import os
from pathlib import Path

# Ensure project root is first in sys.path
_ROOT_DIR = Path(__file__).resolve().parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import socket
import threading
import webbrowser
import time
from loguru import logger

# --- PyInstaller console=False fixes ---
class DummyStream:
    def write(self, *args, **kwargs): pass
    def flush(self, *args, **kwargs): pass
    def isatty(self): return False

if sys.stdout is None: sys.stdout = DummyStream()
if sys.stderr is None: sys.stderr = DummyStream()

if getattr(sys, "frozen", False):
    # Pyinstaller sets PLAYWRIGHT_BROWSERS_PATH=0 which breaks manually installed browsers.
    # We must force it back to the normal Windows AppData location so AIWorker can find the Chromium executable.
    local_app_data = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(local_app_data, "ms-playwright")
# ---------------------------------------

def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _setup_root_logger():
    """Minimal supervisor logger before full config is available."""
    from src.setup.persistence import get_log_path, ensure_dirs
    ensure_dirs()
    logger.remove()
    fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | [Main] {message}"
    # sys.stderr is None when frozen with console=False (no console window)
    if sys.stderr is not None:
        logger.add(sys.stderr, level="INFO", format=fmt)
    try:
        log_path = str(get_log_path())
        logger.add(log_path, rotation="10 MB", level="INFO",
                   encoding="utf-8", format=fmt, enqueue=True)
    except Exception:
        pass


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    # Ensure we run from the project root
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    _setup_root_logger()
    logger.info("=" * 60)
    logger.info("MailAgent starting…")
    logger.info("=" * 60)

    # 1. Start UI server in background thread
    port = _get_free_port()
    from src.ui.server import run_server
    ui_thread = threading.Thread(
        target=run_server, args=(port,), daemon=True, name="UIServer"
    )
    ui_thread.start()
    time.sleep(0.5)  # Wait for server to bind
    ui_url = f"http://127.0.0.1:{port}"
    logger.info(f"UI server started at {ui_url}")

    # 2. Start Supervisor lifecycle watcher in background thread
    from src.setup import persistence
    from src.runtime.supervisor import get_supervisor
    supervisor = get_supervisor()

    def _supervisor_lifecycle():
        try:
            cfg = persistence.load_config()
            if cfg.setup_complete or (cfg.email and cfg.email_template_id):
                if not cfg.setup_complete:
                    cfg.setup_complete = True
                    persistence.save_config(cfg)
                logger.info("Setup complete — starting workers.")
                supervisor.start_all()
            else:
                logger.info("Setup not complete — waiting for UI configuration…")
                while not supervisor.shutdown_event.is_set():
                    time.sleep(2)
                    cfg = persistence.load_config()
                    if cfg.setup_complete or (cfg.email and cfg.email_template_id):
                        if not cfg.setup_complete:
                            cfg.setup_complete = True
                            persistence.save_config(cfg)
                        logger.info("Setup completed via UI — starting workers.")
                        supervisor.start_all()
                        break
        except Exception as e:
            logger.error(f"Supervisor lifecycle error: {e}")

    sup_thread = threading.Thread(
        target=_supervisor_lifecycle, daemon=True, name="SupervisorLifecycle"
    )
    sup_thread.start()

    # 3. Launch Standalone Native Window (pywebview)
    # Resolve icon path — frozen EXE checks _MEIPASS + exe dir; source uses project root
    icon_search_bases = []
    if getattr(sys, 'frozen', False):
        icon_search_bases.append(Path(sys._MEIPASS))
        icon_search_bases.append(Path(sys.executable).parent)  # dist/MailAgent/
        icon_search_bases.append(Path(sys.executable).parent / "_internal")
    else:
        icon_search_bases.append(Path(os.path.dirname(os.path.abspath(__file__))))

    icon_file = None
    for base in icon_search_bases:
        for candidate in ("img/icon.ico", "img/icon.png"):
            p = base / candidate
            if p.exists():
                icon_file = str(p)
                break
        if icon_file:
            break

    logger.info(f"Icon file resolved: {icon_file}")

    try:
        import webview
        logger.info("pywebview loaded — launching standalone window…")

        window = webview.create_window(
            title="MailAgent",
            url=ui_url,
            width=1120,
            height=760,
            min_size=(850, 600),
            background_color="#FAF9F5",
        )
        # webview.start() blocks until the user closes the window
        start_kwargs = {}
        if icon_file:
            start_kwargs["icon"] = icon_file
        
        # We rely on pywebview's internal auto-resolution (edgechromium -> mshtml -> cef)
        # instead of explicitly passing gui="edgechromium" to avoid hard crashes if WebView2 is missing.
        logger.info(f"webview.start() with kwargs: {start_kwargs}")
        webview.start(**start_kwargs)
    except ImportError as e:
        import traceback
        logger.error(f"pywebview is NOT installed — cannot launch standalone window.\n"
                      f"  {traceback.format_exc()}")
        logger.info("Falling back to system browser…")
        webbrowser.open(ui_url)
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    except Exception as e:
        import traceback
        logger.error(f"pywebview standalone window failed: {type(e).__name__}: {e}\n"
                      f"  {traceback.format_exc()}")
        logger.info("Falling back to system browser…")
        webbrowser.open(ui_url)
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    finally:
        logger.info("Closing MailAgent and shutting down all workers…")
        supervisor.stop_all()
        logger.info("MailAgent stopped.")
        # Forcibly terminate the current process and any hanging daemon threads
        os._exit(0)

