"""
In-process rate limiting.

Two things needed a brake:

  1. POST /api/auth/login accepted unlimited guesses. bcrypt's cost was the only
     thing slowing an attacker down (roughly five tries a second), which is not a
     rate limit — it is a speed bump.
  2. /docs, /redoc and /openapi.json are deliberately public, so they need a
     throttle rather than an auth wall.

The limiter is a sliding-window log: per key we keep the timestamps of recent
hits and count the ones still inside the window. That is exact at the window
boundary, unlike a fixed-window counter which allows a double-rate burst across
the boundary.

Scope: this is per-process state. It is the right level for the single-process
deployment UniLLM targets. Behind several replicas each one enforces its own
share of the budget, so set limits accordingly or put a shared limiter in the
reverse proxy.
"""

import os
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional, Tuple

# Cap on tracked keys, so a flood from many source IPs cannot grow the table
# without bound. Least-recently-touched keys are evicted first; evicting a key
# only forgets its history, it never grants access it would otherwise be denied
# for longer than the window.
_MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    """Allow at most `limit` hits per `window` seconds for each key."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _prune(self, times: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while times and times[0] <= cutoff:
            times.popleft()

    def check(self, key: Optional[str]) -> Optional[int]:
        """
        Report whether `key` is currently over its limit, without recording anything.

        Returns None when another hit is allowed, or the number of seconds to wait
        (>= 1) when the limit is exhausted.
        """
        if not self.enabled or key is None:
            return None
        now = time.monotonic()
        with self._lock:
            times = self._hits.get(key)
            if not times:
                return None
            self._prune(times, now)
            if len(times) < self.limit:
                return None
            return max(1, int(self.window - (now - times[0])) + 1)

    def record(self, key: Optional[str]) -> None:
        """Count one hit against `key`."""
        if not self.enabled or key is None:
            return
        now = time.monotonic()
        with self._lock:
            times = self._hits.get(key)
            if times is None:
                times = deque()
                self._hits[key] = times
            self._hits.move_to_end(key)
            self._prune(times, now)
            times.append(now)
            while len(self._hits) > _MAX_TRACKED_KEYS:
                self._hits.popitem(last=False)

    def hit(self, key: Optional[str]) -> Optional[int]:
        """
        Check `key` and, if allowed, record the attempt.

        A rejected attempt is not recorded, so hammering a blocked key does not
        extend the block.
        """
        retry_after = self.check(key)
        if retry_after is None:
            self.record(key)
        return retry_after

    def reset(self, key: Optional[str]) -> None:
        """Forget a key's history — used to clear failures after a successful login."""
        if key is None:
            return
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        """Drop all state (test helper)."""
        with self._lock:
            self._hits.clear()


def parse_limit(value: str, default: Tuple[int, float]) -> Tuple[int, float]:
    """
    Parse a "<count>/<seconds>" limit spec.

    "0", "off", "false" and "none" disable the limiter. Anything unparseable
    falls back to the default rather than failing startup — a malformed env var
    should not take the server down, and the default is the safe direction.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "off", "false", "no", "none", "disabled"):
        return (0, default[1])
    try:
        count_s, _, window_s = raw.partition("/")
        count = int(count_s)
        window = float(window_s) if window_s else default[1]
        if count < 0 or window <= 0:
            raise ValueError
        return (count, window)
    except ValueError:
        from unillm._logging import verbose_proxy_logger
        verbose_proxy_logger.warning(
            f"Ignoring malformed rate limit spec {value!r}; expected '<count>/<seconds>'."
        )
        return default


def _limiter_from_env(env_var: str, default: Tuple[int, float]) -> SlidingWindowLimiter:
    limit, window = parse_limit(os.getenv(env_var, ""), default)
    return SlidingWindowLimiter(limit, window)


# Login attempts per client IP, successes included. Sized so ordinary use (a few
# people sharing an office IP, someone mistyping a password) never trips it,
# while cutting a brute-force from thousands of guesses an hour to tens.
login_ip_limiter = _limiter_from_env("UNILLM_LOGIN_RATE_LIMIT", (30, 60.0))

# Failed attempts per username, across every source IP, so a distributed attack
# cannot dodge the per-IP limit. Cleared on a successful login.
#
# Tradeoff: an attacker who knows a username can keep that account throttled by
# failing on purpose. The window is short and the account is never locked
# permanently, which is the usual balance; set UNILLM_LOGIN_FAILURE_LIMIT=off to
# rely on the per-IP limit alone.
login_user_limiter = _limiter_from_env("UNILLM_LOGIN_FAILURE_LIMIT", (5, 900.0))

# Public docs endpoints, per client IP.
docs_limiter = _limiter_from_env("UNILLM_DOCS_RATE_LIMIT", (30, 60.0))


def reload_from_env() -> None:
    """Re-read limits from the environment (test helper)."""
    global login_ip_limiter, login_user_limiter, docs_limiter
    login_ip_limiter = _limiter_from_env("UNILLM_LOGIN_RATE_LIMIT", (30, 60.0))
    login_user_limiter = _limiter_from_env("UNILLM_LOGIN_FAILURE_LIMIT", (5, 900.0))
    docs_limiter = _limiter_from_env("UNILLM_DOCS_RATE_LIMIT", (30, 60.0))
