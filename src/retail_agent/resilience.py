"""Small, dependency-light resilience primitives shared by every external call.

* `CircuitBreaker` - stops hammering a dependency that is down (fail fast, cheap, clear message),
  half-opens after a cool-down to probe recovery.
* `retry_transient` - tenacity policy: exponential backoff + jitter, only for transient errors.
* `TTLCache` - tiny result cache so identical questions/queries are not paid for twice.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised instead of calling a dependency whose circuit is open."""

    def __init__(self, name: str, retry_in_s: float):
        super().__init__(f"{name} is temporarily unavailable (circuit open, retry in {retry_in_s:.0f}s)")
        self.name = name
        self.retry_in_s = retry_in_s


class CircuitBreaker:
    """Classic closed -> open -> half-open breaker. Thread-safe."""

    def __init__(self, name: str, failure_threshold: int = 4, reset_timeout_s: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if time.monotonic() - self._opened_at >= self.reset_timeout_s:
                return "half_open"
            return "open"

    def before_call(self) -> None:
        if self.state == "open":
            assert self._opened_at is not None
            remaining = self.reset_timeout_s - (time.monotonic() - self._opened_at)
            raise CircuitOpenError(self.name, max(remaining, 0))

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def trip(self) -> None:
        """Open immediately (for failures known to persist: quota exhausted, deadline exceeded)."""
        with self._lock:
            self._failures = self.failure_threshold
            self._opened_at = time.monotonic()

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()

    def call(self, fn: Callable[[], T]) -> T:
        self.before_call()
        try:
            out = fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return out


def retry_transient(is_transient: Callable[[BaseException], bool], attempts: int = 3):
    """Decorator: retry only errors classified as transient, with jittered exponential backoff."""
    return retry(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=0.5, max=8),
        reraise=True,
    )


class TTLCache:
    """LRU + TTL cache (thread-safe)."""

    def __init__(self, max_items: int = 256, ttl_s: float = 600.0):
        self.max_items = max_items
        self.ttl_s = ttl_s
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, value = item
            if time.monotonic() - ts > self.ttl_s:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)
