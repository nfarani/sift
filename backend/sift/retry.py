"""Exponential backoff for provider calls. Retries only TransientError (429 / 5xx)."""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from .providers.base import TransientError

T = TypeVar("T")


class RetryPolicy:
    def __init__(self, max_attempts: int = 6, base: float = 0.5, cap: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, jitter: bool = True):
        self.max_attempts = max_attempts
        self.base = base
        self.cap = cap
        self.sleep = sleep
        self.jitter = jitter

    def delay(self, attempt: int) -> float:
        d = min(self.cap, self.base * (2 ** attempt))
        return d * (0.5 + random.random() / 2) if self.jitter else d

    def call(self, fn: Callable[[], T], on_retry: Callable[[int, Exception], None] | None = None) -> T:
        for attempt in range(self.max_attempts):
            try:
                return fn()
            except TransientError as e:
                if attempt == self.max_attempts - 1:
                    raise
                if on_retry:
                    on_retry(attempt + 1, e)
                self.sleep(self.delay(attempt))
        raise AssertionError("unreachable")
