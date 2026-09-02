"""
Login and public-docs rate limiting.

Before this, POST /api/auth/login accepted unlimited guesses: 30 wrong passwords
in six seconds were all processed, and the correct password still worked
afterwards. bcrypt's cost was the only brake. These tests pin the behaviour that
replaced it, and check the limiter's own edge cases (blocking does not extend the
block, a real login clears the failure history, the limits are configurable off).
"""
import time

import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app
from unillm.proxy import ratelimit
from unillm.proxy.ratelimit import SlidingWindowLimiter, parse_limit
from unillm.db.database import init_db, engine, SessionLocal
from unillm.db.models import Base
from unillm.db import crud
from unillm.proxy.api_routes import hash_password


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def victim(client, db):
    crud.create_user(db=db, username="rl-victim", hashed_password=hash_password("realpass123"))
    return {"username": "rl-victim", "password": "realpass123"}


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _small_limits(monkeypatch, pair=3, ip=100, user=5):
    """
    Install small budgets so a test can exhaust one without running hundreds of
    bcrypt verifications. Returns the limiters it installed.
    """
    limiters = {
        "login_attempt_limiter": SlidingWindowLimiter(pair, 60.0),
        "login_ip_limiter": SlidingWindowLimiter(ip, 60.0),
        "login_user_limiter": SlidingWindowLimiter(user, 900.0),
    }
    for name, limiter in limiters.items():
        monkeypatch.setattr(ratelimit, name, limiter)
    return limiters


# --- the limiter itself --------------------------------------------------------------

def test_limiter_blocks_after_limit_and_reports_retry_after():
    limiter = SlidingWindowLimiter(limit=3, window=60.0)
    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    retry_after = limiter.hit("a")
    assert retry_after is not None and 1 <= retry_after <= 61


def test_limiter_keys_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window=60.0)
    assert limiter.hit("a") is None
    assert limiter.hit("b") is None      # different key, own budget
    assert limiter.hit("a") is not None


def test_blocked_attempts_do_not_extend_the_block():
    """
    A rejected hit must not be recorded. Otherwise an attacker who keeps hammering
    a blocked key rolls the window forward with every try and locks the real owner
    out indefinitely — the limiter would become a self-inflicted denial of service.

    This needs real elapsed time. The bug only shows once the two genuine hits age
    out, so the hammering has to span part of the window and then stop, leaving a
    quiet stretch longer than the window before the final check.
    """
    limiter = SlidingWindowLimiter(limit=2, window=0.5)
    limiter.hit("a")
    limiter.hit("a")
    assert limiter.hit("a") is not None, "a limit of 2 should now be exhausted"

    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        assert limiter.hit("a") is not None, "must stay blocked while the window holds"
        time.sleep(0.01)

    # Quiet for longer than the window: the two genuine hits have expired, and the
    # rejected ones must have left no trace.
    time.sleep(0.35)
    assert limiter.hit("a") is None


def test_reset_clears_history():
    limiter = SlidingWindowLimiter(limit=1, window=60.0)
    limiter.hit("a")
    assert limiter.hit("a") is not None
    limiter.reset("a")
    assert limiter.hit("a") is None


def test_limit_zero_disables_the_limiter():
    limiter = SlidingWindowLimiter(limit=0, window=60.0)
    assert all(limiter.hit("a") is None for _ in range(100))


def test_none_key_is_never_limited():
    """request.client can be absent; an unknown IP must not become a shared bucket."""
    limiter = SlidingWindowLimiter(limit=1, window=60.0)
    assert all(limiter.hit(None) is None for _ in range(10))


@pytest.mark.parametrize("spec,expected", [
    ("10/60", (10, 60.0)),
    ("  5/900 ", (5, 900.0)),
    ("0", (0, 60.0)),
    ("off", (0, 60.0)),
    ("", (30, 60.0)),           # unset → default
    ("garbage", (30, 60.0)),    # unparseable → default, not a crash
    ("-1/60", (30, 60.0)),
    ("10/0", (30, 60.0)),
])
def test_parse_limit(spec, expected):
    assert parse_limit(spec, (30, 60.0)) == expected


def test_a_flood_of_new_keys_cannot_wipe_an_active_block(monkeypatch):
    """
    The eviction table is bounded, and a blocked key used to be the *first* thing
    thrown out of it: rejected attempts are not recorded, so a blocked key stopped
    moving in the least-recently-used order while every new key jumped ahead of it.

    An attacker could exploit that directly — lock an account, then fail logins for
    enough throwaway usernames to push the victim's entry out of the table, and the
    lockout was gone. Blocking has to survive the flood.
    """
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED_KEYS", 50)
    limiter = SlidingWindowLimiter(limit=2, window=900.0)

    limiter.hit("victim")
    limiter.hit("victim")
    assert limiter.check("victim") is not None, "victim starts out blocked"

    for i in range(500):
        limiter.hit(f"throwaway-{i}")
        # The attacker keeps probing the blocked account, as they would when trying
        # to find out whether the lockout has lifted.
        assert limiter.check("victim") is not None, f"block was lost after {i} new keys"


def test_the_key_table_stays_bounded(monkeypatch):
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED_KEYS", 50)
    limiter = SlidingWindowLimiter(limit=2, window=900.0)
    for i in range(500):
        limiter.hit(f"key-{i}")
    assert len(limiter._hits) <= 50


def test_eviction_prefers_unblocked_keys(monkeypatch):
    """A blocked key is only evicted when nothing unblocked is available."""
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED_KEYS", 3)
    limiter = SlidingWindowLimiter(limit=2, window=900.0)

    limiter.hit("blocked")
    limiter.hit("blocked")          # now at its limit
    limiter.hit("idle-a")           # one hit each: not blocked
    limiter.hit("idle-b")
    limiter.hit("newcomer")         # pushes the table over the cap

    assert "blocked" in limiter._hits
    assert limiter.check("blocked") is not None


# --- login endpoint ------------------------------------------------------------------

def test_login_is_rate_limited_per_ip_and_username(client, monkeypatch):
    """Guessing one account from one address runs out of budget."""
    limits = _small_limits(monkeypatch, pair=3)
    assert ratelimit.login_attempt_limiter.limit > 0, "the login limiter must be on by default"

    codes = [_login(client, "nobody", "wrong").status_code for _ in range(limits["login_attempt_limiter"].limit + 2)]
    assert codes[:3] == [401] * 3
    assert codes[3:] == [429] * 2


def test_one_accounts_failures_do_not_lock_out_another_on_the_same_address(client, victim, monkeypatch):
    """
    The reason the budget is keyed on the pair and not the address alone.

    Whenever an address is shared — a NAT, or a reverse proxy whose forwarded
    headers are not trusted — a per-IP-only budget lets one attacker spend it on
    invented usernames and take every real user behind that address offline. Their
    guesses must only cost them their own buckets.
    """
    _small_limits(monkeypatch, pair=3)

    for _ in range(5):
        _login(client, "attackers-target", "wrong")
    assert _login(client, "attackers-target", "wrong").status_code == 429, "attacker is throttled"

    # Same source address, different account: unaffected.
    assert _login(client, victim["username"], victim["password"]).status_code == 200


def test_a_flood_of_invented_usernames_still_meets_the_per_ip_ceiling(client, monkeypatch):
    """
    Splitting the budget by username hands an attacker a fresh bucket per name, so
    a ceiling on the address is what bounds the bcrypt work they can demand.
    """
    _small_limits(monkeypatch, pair=3, ip=6)
    codes = [_login(client, f"nobody-{i}", "wrong").status_code for i in range(8)]
    assert codes[:6] == [401] * 6
    assert codes[6:] == [429] * 2


def test_rate_limited_response_carries_retry_after(client, monkeypatch):
    _small_limits(monkeypatch, pair=2)
    for _ in range(2):
        _login(client, "nobody", "wrong")
    r = _login(client, "nobody", "wrong")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_correct_password_is_refused_while_rate_limited(client, victim, monkeypatch):
    """The whole point: exhausting the budget must stop the attacker mid-search."""
    _small_limits(monkeypatch, pair=3)
    for _ in range(3):
        _login(client, victim["username"], "wrong-password")
    assert _login(client, victim["username"], victim["password"]).status_code == 429


def test_failed_logins_are_limited_per_username_across_ips(client, victim):
    """
    A distributed attacker rotating source IPs must still be stopped, so failures
    are also counted per username.
    """
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    limit = ratelimit.login_user_limiter.limit
    assert limit > 0

    for _ in range(limit):
        # Clear the address-keyed budgets each round to simulate a fresh source.
        ratelimit.login_attempt_limiter.clear()
        ratelimit.login_ip_limiter.clear()
        assert _login(client, victim["username"], "wrong-password").status_code == 401
    ratelimit.login_attempt_limiter.clear()
    ratelimit.login_ip_limiter.clear()
    assert _login(client, victim["username"], "wrong-password").status_code == 429


def test_successful_login_clears_the_failure_counts(client, victim, monkeypatch):
    """A few typos followed by the right password must not leave the user throttled."""
    # Pair budget deliberately above the typo count: this test is about the reset,
    # not about tripping the limiter.
    _small_limits(monkeypatch, pair=6, user=5)
    for _ in range(4):
        _login(client, victim["username"], "wrong-password")
    assert _login(client, victim["username"], victim["password"]).status_code == 200
    # Both address- and username-keyed budgets are back to full: another near-limit
    # run of typos still is not blocked.
    for _ in range(4):
        assert _login(client, victim["username"], "wrong-password").status_code == 401


def test_username_counter_is_case_insensitive(client, victim):
    """Otherwise 'Admin' and 'admin' would each get their own budget."""
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    for _ in range(ratelimit.login_user_limiter.limit):
        ratelimit.login_attempt_limiter.clear()
        ratelimit.login_ip_limiter.clear()
        _login(client, victim["username"].upper(), "wrong-password")
    ratelimit.login_attempt_limiter.clear()
    ratelimit.login_ip_limiter.clear()
    assert _login(client, victim["username"], "wrong-password").status_code == 429


def test_rate_limited_login_is_audited(client, db, monkeypatch):
    _small_limits(monkeypatch, pair=2)
    before, _ = crud.query_audit_logs(db, action="login_rate_limited")
    for _ in range(3):
        _login(client, "nobody", "wrong")
    after, _ = crud.query_audit_logs(db, action="login_rate_limited")
    assert len(after) > len(before)


# --- public docs ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_public_but_throttled(client, path):
    ratelimit.docs_limiter.clear()
    limit = ratelimit.docs_limiter.limit
    assert limit > 0

    # Still public — no credentials, still served.
    assert client.get(path).status_code == 200

    for _ in range(limit - 1):
        client.get(path)
    r = client.get(path)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_docs_throttle_does_not_touch_other_routes(client):
    """The docs budget must not leak into the API or the health check."""
    ratelimit.docs_limiter.clear()
    for _ in range(ratelimit.docs_limiter.limit + 5):
        client.get("/docs")
    assert client.get("/health").status_code == 200
    assert client.get("/api/users/me").status_code == 401  # unauthenticated, not throttled
