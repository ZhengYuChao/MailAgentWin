import pytest
import time
from src.notion.rate_limiter import TokenBucket

def test_token_bucket_acquire():
    # 测试速率限制器是否能按预期限制速率
    limiter = TokenBucket(rate=10, capacity=2)
    
    start_time = time.time()
    # 第一次获取，应该立即成功
    limiter.acquire(1)
    # 第二次获取，应该也立即成功（在容量内）
    limiter.acquire(1)
    
    elapsed = time.time() - start_time
    assert elapsed < 0.1, "First two tokens should be acquired immediately"
    
    # 第三次获取，需要等待令牌桶补充（1/10 秒 = 0.1秒）
    start_time2 = time.time()
    limiter.acquire(1)
    elapsed2 = time.time() - start_time2
    
    assert elapsed2 >= 0.05, "Third token should be delayed by rate limiter"
