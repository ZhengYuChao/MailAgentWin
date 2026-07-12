"""
Notion 限流器：3 RPS 是 Notion 官方上限。预留 buffer 跑 2.5 RPS。
并发 worker 共享同一个桶。
"""
import time
import threading
import logging
from functools import wraps
import requests

log = logging.getLogger(__name__)

class TokenBucket:
    def __init__(self, rate: float = 2.5, capacity: float = 5.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: float = 1.0):
        while True:
            with self.lock:
                now = time.monotonic()
                delta = now - self.last
                # 补充令牌
                self.tokens = min(self.capacity, self.tokens + delta * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                # 计算需要等待的时间
                wait = (n - self.tokens) / self.rate
            time.sleep(wait)

_BUCKET = TokenBucket()

def rate_limited(func):
    """限流装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        _BUCKET.acquire()
        return func(*args, **kwargs)
    return wrapper

class NotionRetrySession(requests.Session):
    """自动处理 429 + 5xx 的退避重试会话"""
    
    def request(self, method, url, **kwargs):
        max_attempts = 6
        backoff = 1.0
        
        for attempt in range(max_attempts):
            _BUCKET.acquire()
            try:
                r = super().request(method, url, **kwargs)
                
                if r.status_code == 429:
                    # 获取重试等待时间，如果没有则使用指数退避
                    retry_after = r.headers.get("Retry-After")
                    wait_time = float(retry_after) if retry_after else backoff
                    log.warning(f"Notion API 429 (Rate Limit): sleeping {wait_time}s (attempt {attempt + 1})")
                    time.sleep(wait_time)
                    backoff *= 2
                    continue
                    
                if 500 <= r.status_code < 600:
                    log.warning(f"Notion API {r.status_code} (Server Error): backoff {backoff}s")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                
                return r
            except requests.RequestException as e:
                if attempt == max_attempts - 1:
                    raise
                log.warning(f"Request failed: {e}, retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
                
        # 应该在循环内 return 或 raise
        return None
