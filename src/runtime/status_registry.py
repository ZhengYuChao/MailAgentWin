"""
Thread-safe worker state registry.
Workers call registry.update() to report their current state.
The UI server calls registry.snapshot() to build the API response.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class WorkerState:
    id: str                      # "supervisor" | "mail" | "ai" | "calendar"
    status: str = "off"          # "starting" | "normal" | "abnormal" | "off"
    pid: int = 0
    task: str = ""               # current task description
    reason: str = ""             # concise error reason (abnormal only)
    progress_current: int = 0
    progress_total: int = 0      # 0 = indeterminate
    retry_count: int = 0
    started_at: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        is_running = self.status in ("normal", "starting") and self.started_at > 0
        uptime_sec = int(time.time() - self.started_at) if is_running else 0
        started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)) if self.started_at > 0 else "-"
        return {
            "id": self.id,
            "status": self.status,
            "pid": self.pid,
            "task": self.task,
            "reason": self.reason,
            "progressCurrent": self.progress_current,
            "progressTotal": self.progress_total,
            "retryCount": self.retry_count,
            "startedAt": int(self.started_at) if self.started_at > 0 else None,
            "startedAtFormatted": started_str,
            "uptimeSeconds": uptime_sec,
        }


class StatusRegistry:
    """Singleton registry for worker and service state."""

    _WORKER_IDS = ("supervisor", "mail", "ai", "calendar")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerState] = {
            wid: WorkerState(id=wid) for wid in self._WORKER_IDS
        }
        self._service_status: str = "stopped"  # "running"|"starting"|"stopped"|"abnormal"

    def update(self, worker_id: str, **kwargs) -> None:
        """Update fields on a worker state. Thread-safe."""
        with self._lock:
            if worker_id not in self._workers:
                return
            ws = self._workers[worker_id]
            old_status = ws.status
            old_pid = ws.pid
            
            new_status = kwargs.get("status", ws.status)
            new_pid = kwargs.get("pid", ws.pid)

            for k, v in kwargs.items():
                if hasattr(ws, k):
                    setattr(ws, k, v)
            
            # Explicit started_at passed in kwargs
            if "started_at" in kwargs:
                pass
            # If status transitioned to "starting" (process launched or restarted):
            elif new_status == "starting":
                ws.started_at = time.time()
            # If PID changed to a new positive PID while running:
            elif new_pid > 0 and old_pid > 0 and new_pid != old_pid:
                ws.started_at = time.time()
            # If status transitioned from off/abnormal to normal:
            elif new_status == "normal" and (old_status in ("off", "abnormal") or ws.started_at == 0.0):
                ws.started_at = time.time()
            # If stopped:
            elif new_status == "off":
                ws.started_at = 0.0

            ws.updated_at = time.time()
            # Auto-update service status
            self._recalc_service_status()

    def set_service_status(self, status: str) -> None:
        with self._lock:
            self._service_status = status

    def _recalc_service_status(self) -> None:
        """Derive overall service status from individual worker states."""
        statuses = [ws.status for ws in self._workers.values() if ws.status != "off"]
        if not statuses:
            self._service_status = "stopped"
        elif all(s == "normal" for s in statuses):
            self._service_status = "running"
        elif any(s == "starting" for s in statuses):
            self._service_status = "starting"
        elif any(s == "abnormal" for s in statuses):
            self._service_status = "abnormal"
        else:
            self._service_status = "running"

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of all state."""
        with self._lock:
            return {
                "serviceStatus": self._service_status,
                "workers": [ws.to_dict() for ws in self._workers.values()],
            }

    def snapshot_list(self) -> list:
        """Return just the workers list."""
        with self._lock:
            return [ws.to_dict() for ws in self._workers.values()]

    def reset_all(self) -> None:
        """Reset all workers to 'off' state (called before fresh start)."""
        with self._lock:
            for wid in self._WORKER_IDS:
                self._workers[wid] = WorkerState(id=wid)
            self._service_status = "stopped"


# Module-level singleton
registry = StatusRegistry()
