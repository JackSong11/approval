"""Retry helpers — exponential backoff for transient API failures. Retry with exponential backoff"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

from approval.ui import print_retry

# # ─── Retry with exponential backoff ──────────────────────────

T = TypeVar("T")

# HTTP 状态码或异常文本满足下列条件时，认为是可重试的瞬时错误
_RETRYABLE_STATUS = {429, 503, 529}
_RETRYABLE_KEYWORDS = ("overloaded", "ECONNRESET", "ETIMEDOUT")


def is_retryable(error: Exception) -> bool:
    """判断错误是否值得重试（限流、过载、网络抖动等）。"""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in _RETRYABLE_STATUS:
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


async def with_retry(fn: Callable[[], Awaitable[T]], max_retries: int = 3) -> T:
    """以指数退避方式重试一个异步可调用。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not is_retryable(error):
                raise
            # 退避时间 = min(2^attempt 秒, 30 秒) + 抖动
            # 指数退避 (2  attempt)： 等待时间随重试次数呈指数增长。第一次失败可能等 1 秒，第二次等 2 秒，第三次等 4 秒……以此类推。
            # 设定上限 (min(..., 30000))： 限制最大等待时间不超过 30 秒，防止等太久。
            # 引入抖动（Jitter）： 后面加的那串 hash(...) 随机数，是给等待时间加了一点“随机噪音”。这样可以防止很多并发任务在同一时间一起重试，从而把服务器再次压垮。
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)
    # 理论上不可达；仅为类型检查
    raise RuntimeError("with_retry exhausted without returning")
