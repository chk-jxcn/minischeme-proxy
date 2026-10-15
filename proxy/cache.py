"""In-memory caching system with TTL expiration for proxy endpoints."""

import threading
import time
from typing import Any, Optional, Dict, Tuple


class TTLCache:
    def __init__(self):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            if key in self._cache:
                exp, val = self._cache[key]
                if exp > now:
                    return val
                del self._cache[key]
        return None

    def set(self, key: str, val: Any, ttl_seconds: float):
        now = time.time()
        with self._lock:
            self._cache[key] = (now + ttl_seconds, val)

    def invalidate(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()
