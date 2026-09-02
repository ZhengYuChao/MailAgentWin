"""
MailAgent supervisor — wraps ProcessManager with status registry integration.
Provides start/stop/restart API for individual workers.
"""
from __future__ import annotations
import multiprocessing
import sys
import time
import os
import threading
from loguru import logger

from src.runtime.status_registry import registry


class Supervisor:
    """
    Wraps the existing ProcessManager logic and adds:
    - Status registry reporting
    - Per-worker start/stop/restart via API
    - setupComplete gating (waits for UI config before starting workers)
    """

    MAX_BACKOFF = 60
    INITIAL_BACKOFF = 5
    CHECK_INTERVAL = 2
    GRACEFUL_TIMEOUT = 10

    def __init__(self) -> None:
        self.ai_trigger_queue = multiprocessing.Queue()
        self.shutdown_event = multiprocessing.Event()
        self._proc_mail = None
        self._proc_ai = None
        self._backoff_mail = self.INITIAL_BACKOFF
        self._backoff_ai = self.INITIAL_BACKOFF
        self._monitor_thread: threading.Thread | None = None
        self._auto_restart: bool = True

    # ── Internal process start helpers ───────────────────────────────────────

    def _start_mail_worker(self) -> None:
        from workers.mail_worker import run_mail_worker
        start_ts = time.time()
        registry.update("mail", status="starting", task="Initializing", reason="", pid=0, started_at=start_ts)
        self._proc_mail = multiprocessing.Process(
            target=run_mail_worker,
            args=(self.ai_trigger_queue, self.shutdown_event),
            name="MailWorker",
        )
        self._proc_mail.start()
        registry.update("mail", pid=self._proc_mail.pid, started_at=start_ts)
        logger.info(f"🚀 Started MailWorker (PID: {self._proc_mail.pid})")

    def _start_ai_worker(self) -> None:
        from workers.ai_worker import run_ai_worker
        from src.setup.persistence import get_browser_dir
        auth_path = get_browser_dir() / "notion_auth.json"
        if not auth_path.exists():
            registry.update(
                "ai",
                status="abnormal",
                task="",
                reason="Sign-in required",
                started_at=0.0,
                pid=0,
            )
            logger.warning("AIWorker not started: notion_auth.json missing. Sign in via Settings > AI.")
            return
        start_ts = time.time()
        registry.update("ai", status="starting", task="Initializing", reason="", pid=0, started_at=start_ts)
        self._proc_ai = multiprocessing.Process(
            target=run_ai_worker,
            args=(self.ai_trigger_queue, self.shutdown_event),
            name="AIWorker",
        )
        self._proc_ai.start()
        registry.update("ai", pid=self._proc_ai.pid, started_at=start_ts)
        logger.info(f"🚀 Started AIWorker (PID: {self._proc_ai.pid})")

    def _start_calendar_worker(self) -> None:
        """Calendar worker runs inside MailWorker process; this just sets status."""
        from src.setup.persistence import load_config
        cfg = load_config()
        if not cfg.calendar_enabled:
            registry.update("calendar", status="off", task="Not configured", reason="", started_at=0.0, pid=0)
        else:
            registry.update("calendar", status="normal", task="Calendar sync active", reason="", started_at=time.time())
        # Calendar sync is started by mail_worker.py via asyncio.create_task

    # ── Public API ────────────────────────────────────────────────────────────

    def start_all(self) -> None:
        """Start all workers and begin liveness monitoring."""
        self.shutdown_event.clear()
        registry.update("supervisor", status="normal", pid=os.getpid(), started_at=time.time(), task="Monitoring active processes", reason="")
        self._start_mail_worker()
        self._start_ai_worker()
        self._start_calendar_worker()
        self._start_monitor()
        logger.info("All workers started.")

    def restart_worker(self, worker_id: str) -> dict:
        """Restart a specific worker by ID. Returns result dict."""
        if worker_id == "mail":
            registry.update("mail", status="starting", pid=0, started_at=time.time(), task="Restarting", reason="")
            if self._proc_mail and self._proc_mail.is_alive():
                self._proc_mail.terminate()
                self._proc_mail.join(timeout=self.GRACEFUL_TIMEOUT)
            self._start_mail_worker()
            return {"ok": True, "message": "MailWorker restarting."}

        elif worker_id == "ai":
            registry.update("ai", status="starting", pid=0, started_at=time.time(), task="Restarting", reason="")
            if self._proc_ai and self._proc_ai.is_alive():
                self._proc_ai.terminate()
                self._proc_ai.join(timeout=self.GRACEFUL_TIMEOUT)
            self._start_ai_worker()
            return {"ok": True, "message": "AIWorker restarting."}

        elif worker_id == "calendar":
            from src.setup.persistence import load_config
            cfg = load_config()
            if not cfg.calendar_enabled:
                return {"ok": False, "message": "Calendar is not configured."}
            # Calendar runs in MailWorker — restart mail worker
            registry.update("calendar", status="starting", pid=0, started_at=time.time(), task="Restarting", reason="")
            if self._proc_mail and self._proc_mail.is_alive():
                self._proc_mail.terminate()
                self._proc_mail.join(timeout=self.GRACEFUL_TIMEOUT)
            self._start_mail_worker()
            return {"ok": True, "message": "Calendar Worker restarting (via MailWorker)."}

        return {"ok": False, "message": f"Unknown worker id: {worker_id}"}

    def stop_all(self) -> None:
        """Gracefully shut down all workers."""
        logger.info("Supervisor: shutting down all workers…")
        self.shutdown_event.set()
        registry.update("supervisor", status="off", pid=0, task="Stopped", reason="")
        for name, proc in [("MailWorker", self._proc_mail), ("AIWorker", self._proc_ai)]:
            if proc and proc.is_alive():
                proc.join(timeout=self.GRACEFUL_TIMEOUT)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
                    if proc.is_alive():
                        proc.kill()
        registry.set_service_status("stopped")
        logger.info("All workers stopped.")

    def trigger_force_sync(self, days: int) -> None:
        """Trigger a force sync of the last N days."""
        logger.info(f"Supervisor: Triggering force sync for last {days} days...")
        
        def _sync_thread():
            # Stop existing processes
            self.stop_all()
            
            # Wait a moment for processes to fully exit
            time.sleep(2)
            
            # Update registry status
            registry.update("supervisor", status="syncing", task=f"Force syncing last {days} days", reason="",
                            progress_current=0, progress_total=0)
            registry.set_service_status("syncing")
            
            try:
                self._run_force_sync(days)
            except Exception as e:
                logger.error(f"Supervisor: Force sync failed: {e}")
                registry.update("supervisor", status="abnormal", task="Force sync failed",
                                reason=str(e)[:120], progress_current=0, progress_total=0)
            
            # Start everything back up
            logger.info("Supervisor: Restarting all workers after force sync...")
            self.start_all()
        
        threading.Thread(target=_sync_thread, daemon=True, name="ForceSyncThread").start()

    def _run_force_sync(self, days: int) -> None:
        """Execute the force sync: scan Outlook folders and sync each email to Notion."""
        import asyncio
        from datetime import datetime, timedelta

        async def _async_sync():
            from src.mail.outlook_com_arm import OutlookComArm, OL_FOLDER_INBOX, OL_FOLDER_SENT
            from src.mail.new_watcher_win import WindowsWatcher
            from src.mail.sync_store import SyncStore

            logger.info(f"[FORCE-SYNC] Starting force sync for the last {days} days...")
            registry.update("supervisor", task="Force sync: initializing Outlook COM…")

            sync_store = SyncStore()
            watcher = WindowsWatcher()
            since = datetime.now() - timedelta(days=days)

            # Phase 1: Scan folders
            logger.info("[FORCE-SYNC] Phase 1: Scanning Outlook folders...")
            registry.update("supervisor", task="Force sync: scanning Inbox…")
            inbox_items = watcher.arm.iter_folder(OL_FOLDER_INBOX, since=since)
            logger.info(f"[FORCE-SYNC] Found {len(inbox_items)} Inbox items.")

            registry.update("supervisor", task="Force sync: scanning Sent Items…")
            sent_items = watcher.arm.iter_folder(OL_FOLDER_SENT, since=since)
            logger.info(f"[FORCE-SYNC] Found {len(sent_items)} Sent items.")

            all_items = inbox_items + sent_items

            total = len(all_items)
            logger.info(f"[FORCE-SYNC] Phase 2: Processing {total} emails (syncing new & updating timestamps) …")
            registry.update("supervisor", task=f"Force sync: 0/{total} emails processed",
                            progress_current=0, progress_total=total)

            if total == 0:
                logger.info("[FORCE-SYNC] ✅ Nothing to sync — no emails found.")
                registry.update("supervisor", task="Force sync complete (0 items)",
                                progress_current=0, progress_total=0)
                await watcher.close()
                return

            # Phase 2: Sync new emails & update existing dates
            synced_new = 0
            updated_dates = 0
            failed = 0
            for idx, (eid, sid) in enumerate(all_items, 1):
                try:
                    if not sync_store.is_synced(eid):
                        await watcher.process_mail_sync(entry_id=eid, store_id=sid, trigger_ai=False)
                        synced_new += 1
                    else:
                        # Update Date property on existing Notion page to ensure correct local timezone
                        page_id = sync_store.get_page_id(eid)
                        if page_id:
                            fetched = watcher.arm.get_mail_by_id(eid, sid)
                            if fetched and fetched.date_utc:
                                await watcher.notion_sync.client.client.pages.update(
                                    page_id=page_id,
                                    properties={"Date": {"date": {"start": fetched.date_utc}}}
                                )
                                updated_dates += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"[FORCE-SYNC] Failed processing {eid[:24]}: {e}")

                # Update progress
                registry.update("supervisor",
                                task=f"Force sync: {idx}/{total} emails processed",
                                progress_current=idx, progress_total=total)
                
                if idx % 10 == 0 or idx == total:
                    logger.info(f"[FORCE-SYNC] Progress: {idx}/{total} "
                                f"(new={synced_new}, date_updated={updated_dates}, failed={failed})")

            await watcher.close()
            logger.info(f"[FORCE-SYNC] ✅ Complete. New synced: {synced_new}, "
                         f"Dates updated: {updated_dates}, Failed: {failed}")
            registry.update("supervisor",
                            task=f"Force sync complete ({synced_new} new, {updated_dates} updated)",
                            progress_current=total, progress_total=total)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_async_sync())
        finally:
            loop.close()

    # ── Monitor loop ─────────────────────────────────────────────────────────

    def _start_monitor(self) -> None:
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="SupervisorMonitor"
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        """Watch worker processes and auto-restart on crash."""
        from src.setup.persistence import load_config
        while not self.shutdown_event.is_set():
            cfg = load_config()
            auto_restart = getattr(cfg, "workers_auto_restart", True)

            registry.update("supervisor", status="normal", pid=os.getpid(), task="Monitoring active processes", reason="")

            # Check MailWorker
            if self._proc_mail:
                if not self._proc_mail.is_alive():
                    code = self._proc_mail.exitcode
                    logger.error(f"❌ MailWorker crashed (exit code: {code}).")
                    registry.update("mail", status="abnormal", pid=0, reason=f"Crashed (exit {code})")
                    if auto_restart:
                        logger.info(f"Restarting MailWorker in {self._backoff_mail}s…")
                        time.sleep(self._backoff_mail)
                        if not self.shutdown_event.is_set():
                            self._start_mail_worker()
                            self._backoff_mail = min(self._backoff_mail * 2, self.MAX_BACKOFF)
                else:
                    self._backoff_mail = self.INITIAL_BACKOFF
                    registry.update("mail", status="normal", pid=self._proc_mail.pid, task="Watching inbox & syncing", reason="")

            # Check AIWorker
            if self._proc_ai:
                if not self._proc_ai.is_alive():
                    code = self._proc_ai.exitcode
                    logger.error(f"❌ AIWorker crashed (exit code: {code}).")
                    registry.update("ai", status="abnormal", pid=0, reason=f"Crashed (exit {code})")
                    if auto_restart:
                        logger.info(f"Restarting AIWorker in {self._backoff_ai}s…")
                        time.sleep(self._backoff_ai)
                        if not self.shutdown_event.is_set():
                            self._start_ai_worker()
                            self._backoff_ai = min(self._backoff_ai * 2, self.MAX_BACKOFF)
                else:
                    self._backoff_ai = self.INITIAL_BACKOFF
                    registry.update("ai", status="normal", pid=self._proc_ai.pid, task="Listening for triggers & digests", reason="")

            # Check Calendar
            if cfg.calendar_enabled:
                if self._proc_mail and self._proc_mail.is_alive():
                    registry.update("calendar", status="normal", pid=self._proc_mail.pid, task="Calendar sync active", reason="")
            else:
                registry.update("calendar", status="off", pid=0, task="Disabled", reason="")

            time.sleep(self.CHECK_INTERVAL)


# Module-level singleton
_supervisor: Supervisor | None = None
_supervisor_lock = threading.Lock()


def get_supervisor() -> Supervisor:
    global _supervisor
    with _supervisor_lock:
        if _supervisor is None:
            _supervisor = Supervisor()
    return _supervisor
