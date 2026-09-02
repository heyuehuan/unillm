"""
In-process rate limiting.

Two things needed a brake:

  1. POST /api/auth/login accepted unlimited guesses. bcrypt's cost was the only
     thing slowing an attacker down (roughly five tries a second), which is not a
     rate limit — it is a speed bump.
  2. /docs, /redoc and /openapi.json are deliberately public, so they need a
     throttle rather than an auth wall.

Login uses three budgets, because no single key is both precise and complete:

  * per (IP, username) — the everyday brake on guessing one account from one place
  * per IP             — a ceiling on how much bcrypt one address can demand
  * per username       — catches the distributed attack that rotates addresses

The pair budget is the tight one. A bare per-IP budget cannot tell an attacker's
invented usernames apart from the real users sharing that address, so spending it
locks the real users out; splitting by username means a flood only exhausts the
buckets it names.

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
# without bound. Least-recently-touched keys are evicted first.
#
# Evicting a key forgets its history, which for a key that is *currently blocked*
# means handing back a budget it had already spent. So eviction skips blocked keys
# while any unblocked key remains: the table stays bounded, but a flood of new keys
# can no longer be used to wipe somebody's lockout. Only when every candidate is
# blocked does it fall back to plain least-recently-used.
_MAX_TRACKED_KEYS = 10_000

# How far to look for an unblocked key before giving up and evicting the oldest.
# Bounded so a table of blocked keys cannot make each insert a full scan.
_EVICTION_SCAN_LIMIT = 64


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
            # Consulting a key counts as touching it. Without this a blocked key
            # never moves — rejected attempts are deliberately not recorded — so it
            # would drift to the front of the eviction order and an attacker could
            # flush their own lockout by filling the table with fresh keys.
            self._hits.move_to_end(key)
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
            self._evict_over_cap(now)

    def _evict_over_cap(self, now: float) -> None:
        """Trim the table back to the cap, sparing blocked keys. Caller holds the lock."""
        while len(self._hits) > _MAX_TRACKED_KEYS:
            evict = None
            for scanned, (key, times) in enumerate(self._hits.items()):
                if scanned >= _EVICTION_SCAN_LIMIT:
                    break
                self._prune(times, now)
                if len(times) < self.limit:
                    evict = key
                    break
            if evict is None:
                # Everything in range is still blocked; the cap wins over the block.
                evict = next(iter(self._hits))
            del self._hits[evict]

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


# Login attempts per (client IP, username) pair, successes included. This is the
# precise brake: it stops one source hammering one account, without that flood
# touching anyone else who logs in from the same address.
#
# Keying on the pair rather than the address alone matters whenever an address is
# shared — a NAT, an office egress, or (the common accident) a reverse proxy whose
# forwarded headers UniLLM is not configured to trust. With a bare per-IP counter,
# an attacker spending the budget on invented usernames locks out every real user
# behind that address; here their guesses only consume the buckets they name.
login_attempt_limiter = _limiter_from_env("UNILLM_LOGIN_RATE_LIMIT", (30, 60.0))

# Login attempts per client IP regardless of username. The pair limiter above lets
# an attacker open a fresh bucket per invented username, so something still has to
# bound the bcrypt work one address can demand. Set well above what a shared office
# address plausibly generates, since exhausting it does block everyone behind it.
login_ip_limiter = _limiter_from_env("UNILLM_LOGIN_IP_RATE_LIMIT", (100, 60.0))

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


def login_pair_key(ip: Optional[str], username_key: str) -> Optional[str]:
    """
    Key for the per-(IP, username) budget, or None when the address is unknown —
    an unknown address must not collapse every caller into one shared bucket.
    """
    if ip is None:
        return None
    return f"{ip}\x00{username_key}"


def reload_from_env() -> None:
    """Re-read limits from the environment (test helper)."""
    global login_attempt_limiter, login_ip_limiter, login_user_limiter, docs_limiter
    login_attempt_limiter = _limiter_from_env("UNILLM_LOGIN_RATE_LIMIT", (30, 60.0))
    login_ip_limiter = _limiter_from_env("UNILLM_LOGIN_IP_RATE_LIMIT", (100, 60.0))
    login_user_limiter = _limiter_from_env("UNILLM_LOGIN_FAILURE_LIMIT", (5, 900.0))
    docs_limiter = _limiter_from_env("UNILLM_DOCS_RATE_LIMIT", (30, 60.0))
