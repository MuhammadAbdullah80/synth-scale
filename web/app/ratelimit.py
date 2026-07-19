"""In-memory sliding-window rate limiter.

LIMITATION: state lives in this process only. Behind multiple uvicorn/gunicorn
workers each worker has its own window (so the effective limit is
limit x workers), and a restart clears everything. Fine for a single-worker
free-tier deploy; swap for Redis or a proxy-level limit if you scale out.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, limit: int = 10, window_seconds: float = 3600.0):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit for `key`. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            cutoff = now - self.window
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                retry_after = int(q[0] + self.window - now) + 1
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
