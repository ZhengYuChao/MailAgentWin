import queue
import threading
import time
from typing import Optional
from loguru import logger
from src.models import Task, TaskType, TaskPriority

class TaskPool:
    """
    全局任务池，单例模式。
    基于线程安全的 PriorityQueue。
    对 MAIL_SYNC 类型的任务，通过 entry_id 去重防止同一封邮件被重复入队。
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TaskPool, cls).__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.queue = queue.PriorityQueue()
        self._pending_entry_ids: set[str] = set()
        self._pending_lock = threading.Lock()

    def add_task(self, task_type: TaskType, priority: TaskPriority, payload: dict, timestamp: Optional[float] = None):
        """
        添加任务到队列中。
        :param task_type: 任务类型
        :param priority: 任务优先级
        :param payload: 任务负载字典
        :param timestamp: 任务关联的时间戳，默认为当前时间。
        """
        # MAIL_SYNC 任务按 entry_id 去重
        if task_type == TaskType.MAIL_SYNC:
            entry_id = payload.get("entry_id", "")
            if entry_id:
                with self._pending_lock:
                    if entry_id in self._pending_entry_ids:
                        logger.debug(f"Skipped duplicate MAIL_SYNC task: {entry_id[:24]}")
                        return
                    self._pending_entry_ids.add(entry_id)

        if timestamp is None:
            timestamp = time.time()
            
        # LIFO: 使用负时间戳，使得时间越晚的任务，负值越小，越容易出队。
        timestamp_desc = -timestamp

        task = Task(
            priority_level=priority.value,
            timestamp_desc=timestamp_desc,
            type=task_type,
            payload=payload
        )
        self.queue.put(task)
        logger.debug(f"Task added: {task_type.name} (Priority {priority.name}, TS: {timestamp})")

    def mark_entry_done(self, entry_id: str):
        """任务处理完成后释放 entry_id，允许未来重新入队（如兜底重试）"""
        with self._pending_lock:
            self._pending_entry_ids.discard(entry_id)

    def get_task_nowait(self) -> Optional[Task]:
        """非阻塞获取任务"""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def peek_task(self) -> Optional[Task]:
        """查看队列顶部的最高优先级任务而不出队"""
        with self.queue.mutex:
            if self.queue.queue:
                return self.queue.queue[0]
            return None

    def task_done(self):
        """标记任务完成"""
        self.queue.task_done()

    def qsize(self) -> int:
        return self.queue.qsize()

# 暴露一个全局单例实例
global_task_pool = TaskPool()
