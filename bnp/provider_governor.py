from __future__ import annotations
import random, threading, time
from dataclasses import dataclass

@dataclass
class ProviderPolicy:
    min_interval: float = 1.0
    burst: int = 1
    daily_budget: int | None = None
    max_backoff: float = 60.0

class ProviderGovernor:
    def __init__(self, policy: ProviderPolicy):
        self.policy = policy
        self._lock = threading.Lock()
        self._last = 0.0
        self._calls = 0
        self._day = time.strftime("%Y-%m-%d", time.gmtime())

    @property
    def calls(self) -> int:
        return self._calls

    def acquire(self):
        # Do not hold the lock while sleeping: other UI/status threads may inspect state.
        with self._lock:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self._day:
                self._day, self._calls = day, 0
            if self.policy.daily_budget is not None and self._calls >= self.policy.daily_budget:
                raise RuntimeError("provider request budget exhausted")
            wait = self.policy.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._last = time.monotonic()
            self._calls += 1

    def backoff(self, attempt: int, retry_after: float | None = None):
        delay = max(0.0, retry_after) if retry_after is not None else 2 ** max(0, attempt) + random.random()
        time.sleep(min(self.policy.max_backoff, delay))
