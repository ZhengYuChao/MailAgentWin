"""
MailAgent 进程管理器 (Supervisor)
负责启动、监控和自动重启两个工作进程：
  - MailWorker (进程 A): Outlook COM 邮件监听 + Webhook Server + Notion 同步
  - AIWorker  (进程 B): Notion AI Chat 自动化（Playwright + 防抖 + 定时调度）
"""
import multiprocessing
import sys
import time
import os
from loguru import logger


def _setup_supervisor_logger():
    """配置 Supervisor 进程的日志"""
    logger.remove()
    fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | [Supervisor] {message}"
    logger.add(sys.stderr, level="INFO", format=fmt)
    logger.add("logs/mailagent.log", rotation="10 MB", level="DEBUG",
               encoding="utf-8", format=fmt, enqueue=True)


class ProcessManager:
    """管理 MailWorker 和 AIWorker 两个子进程的 Supervisor"""

    MAX_BACKOFF = 60       # 最大重启退避间隔(秒)
    INITIAL_BACKOFF = 5    # 初始重启退避间隔(秒)
    CHECK_INTERVAL = 2     # 存活检查间隔(秒)
    GRACEFUL_TIMEOUT = 10  # 优雅停止超时(秒)

    def __init__(self):
        self.ai_trigger_queue = multiprocessing.Queue()
        self.shutdown_event = multiprocessing.Event()
        self.proc_a = None  # MailWorker
        self.proc_b = None  # AIWorker
        self._backoff_a = self.INITIAL_BACKOFF
        self._backoff_b = self.INITIAL_BACKOFF

    def _start_mail_worker(self):
        """启动 MailWorker 子进程"""
        from workers.mail_worker import run_mail_worker
        self.proc_a = multiprocessing.Process(
            target=run_mail_worker,
            args=(self.ai_trigger_queue, self.shutdown_event),
            name="MailWorker",
        )
        self.proc_a.start()
        logger.info(f"🚀 Started MailWorker (PID: {self.proc_a.pid})")

    def _start_ai_worker(self):
        """启动 AIWorker 子进程"""
        from workers.ai_worker import run_ai_worker
        self.proc_b = multiprocessing.Process(
            target=run_ai_worker,
            args=(self.ai_trigger_queue, self.shutdown_event),
            name="AIWorker",
        )
        self.proc_b.start()
        logger.info(f"🚀 Started AIWorker (PID: {self.proc_b.pid})")

    def run(self):
        """主入口：启动子进程并进入监控循环"""
        _setup_supervisor_logger()
        logger.info("=" * 60)
        logger.info("MailAgent Process Manager (Supervisor) starting...")
        logger.info("=" * 60)

        # 前置检查
        if not os.path.exists("notion_auth.json"):
            logger.error("❌ notion_auth.json not found! Please run 'python notion_auth.py' first to authenticate with Notion AI.")
            sys.exit(1)

        self._start_mail_worker()
        self._start_ai_worker()

        try:
            self._monitor_loop()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received.")
        finally:
            self._shutdown()

    def _monitor_loop(self):
        """监控子进程存活状态，crash 后自动重启（带指数退避）"""
        while not self.shutdown_event.is_set():
            # 监控 MailWorker (进程 A)
            if self.proc_a and not self.proc_a.is_alive():
                exit_code = self.proc_a.exitcode
                logger.error(f"❌ MailWorker crashed (exit code: {exit_code}). "
                             f"Restarting in {self._backoff_a}s...")
                time.sleep(self._backoff_a)
                if not self.shutdown_event.is_set():
                    self._start_mail_worker()
                    self._backoff_a = min(self._backoff_a * 2, self.MAX_BACKOFF)
            else:
                self._backoff_a = self.INITIAL_BACKOFF  # 正常运行则重置退避

            # 监控 AIWorker (进程 B)
            if self.proc_b and not self.proc_b.is_alive():
                exit_code = self.proc_b.exitcode
                logger.error(f"❌ AIWorker crashed (exit code: {exit_code}). "
                             f"Restarting in {self._backoff_b}s...")
                time.sleep(self._backoff_b)
                if not self.shutdown_event.is_set():
                    self._start_ai_worker()
                    self._backoff_b = min(self._backoff_b * 2, self.MAX_BACKOFF)
            else:
                self._backoff_b = self.INITIAL_BACKOFF

            time.sleep(self.CHECK_INTERVAL)

    def _shutdown(self):
        """优雅终止所有子进程"""
        logger.info("Shutting down all processes...")
        self.shutdown_event.set()

        for name, proc in [("MailWorker", self.proc_a), ("AIWorker", self.proc_b)]:
            if proc and proc.is_alive():
                logger.info(f"Waiting for {name} (PID: {proc.pid}) to stop gracefully...")
                proc.join(timeout=self.GRACEFUL_TIMEOUT)
                if proc.is_alive():
                    logger.warning(f"⚠️ {name} did not stop gracefully, terminating...")
                    proc.terminate()
                    proc.join(timeout=5)
                    if proc.is_alive():
                        logger.warning(f"⚠️ Force killing {name}...")
                        proc.kill()

        logger.info("=" * 60)
        logger.info("MailAgent stopped.")
        logger.info("=" * 60)
