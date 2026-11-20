from __future__ import annotations

import functools
import logging
import random
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG_INFRA = logging.getLogger("engine.infra")


class RateLimiter:
    """Token bucket, thread-safe, one bucket per API group, multiple windows per group.

    Brokers typically enforce a burst limit and a per-minute ceiling.
    """

    def __init__(self, limits: Dict[str, Any]):
        # group -> [(max_calls, per_seconds), ...]
        self.limits: Dict[str, List[Tuple[int, float]]] = {
            k: (list(v) if isinstance(v, list) else [v]) for k, v in limits.items()
        }
        self._hits: Dict[str, deque] = defaultdict(deque)
        self._cooldown: Dict[str, float] = {}
        self._lock = threading.Lock()

    def penalise(self, group: str, seconds: float) -> None:
        """Called when the API says we breached. Freezes the whole group, not just one thread."""
        with self._lock:
            self._cooldown[group] = max(self._cooldown.get(group, 0.0), time.monotonic() + seconds)
        LOG_INFRA.warning("rate limit hit on '%s' — freezing that group for %.0fs", group, seconds)

    def acquire(self, group: str = "default") -> None:
        rules = self.limits.get(group) or self.limits.get("default", [(8, 1.0)])
        longest = max(w for _, w in rules)
        while True:
            with self._lock:
                cool = self._cooldown.get(group, 0.0) - time.monotonic()
            if cool > 0:
                LOG_INFRA.debug("'%s' group cooling down — %.1fs left", group, cool)
                time.sleep(min(cool, 5.0))
                continue
            with self._lock:
                now = time.monotonic()
                q = self._hits[group]
                while q and now - q[0] > longest:
                    q.popleft()
                wait = 0.0
                for max_calls, window in rules:
                    in_window = [t for t in q if now - t <= window]
                    if len(in_window) >= max_calls:
                        wait = max(wait, window - (now - in_window[0]) + 0.01)
                if wait <= 0:
                    q.append(now)
                    return
            if wait > 2.0:
                LOG_INFRA.debug("'%s' bucket full — waiting %.1fs", group, wait)
            time.sleep(min(wait, 5.0))


LIMITER = RateLimiter({
    "default":     [(8, 1.0)],
    "orders":      [(8, 1.0), (200, 60.0)],
    "live_data":   [(8, 1.0), (250, 60.0)],
    "historical":  [(1, 1.0), (50, 60.0)],
    "non_trading": [(8, 1.0), (400, 60.0)],
})

RATE_LIMIT_HINTS = ("rate limit", "too many requests", "429")
HARD_FAIL_HINTS = ("invalid", "unauthor", "forbidden", "rejected", "maximum of", "not found")


def retry(times: int = 3, base: float = 0.8, group: str = "default", idempotent: bool = True):
    """Exponential backoff with full jitter. Rate-limit errors get a long, escalating sleep
    rather than a 300ms jitter; hard failures short-circuit. Non-idempotent calls: one attempt.
    """
    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempts = times if idempotent else 1
            last = None
            for i in range(attempts):
                try:
                    LIMITER.acquire(group)
                    return fn(*args, **kwargs)
                except Exception as e:
                    last = e
                    msg = str(e).lower()
                    if any(k in msg for k in RATE_LIMIT_HINTS):
                        LIMITER.penalise(group, 20.0 * (2 ** i))
                        time.sleep(random.uniform(0, 2.0))
                        continue
                    if any(k in msg for k in HARD_FAIL_HINTS):
                        LOG_INFRA.debug("%s: hard failure, not retrying — %s", fn.__name__, str(e)[:160])
                        break
                    if i < attempts - 1:
                        sleep = random.uniform(0, base * (2 ** i))
                        LOG_INFRA.debug("%s: attempt %d/%d failed (%s) — retrying in %.2fs",
                                        fn.__name__, i + 1, attempts, str(e)[:120], sleep)
                        time.sleep(sleep)
            LOG_INFRA.log(logging.WARNING if group == "orders" else logging.DEBUG,
                          "%s failed after %d attempt(s): %s", fn.__name__, attempts, last)
            raise last
        return wrapper
    return deco
