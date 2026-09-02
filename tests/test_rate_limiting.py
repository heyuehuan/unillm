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


# --- login endpoint ------------------------------------------------------------------

def test_login_is_rate_limited_per_ip(client, victim):
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    limit = ratelimit.login_ip_limiter.limit
    assert limit > 0, "the per-IP login limiter must be on by default"

    # Spread failures over distinct usernames so the per-username limiter is not
    # what stops us — this test is specifically about the per-IP budget.
    codes = [_login(client, f"nobody-{i}", "wrong").status_code for i in range(limit + 3)]
    assert codes[:limit] == [401] * limit
    assert codes[limit:] == [429] * 3


def test_rate_limited_response_carries_retry_after(client):
    ratelimit.login_ip_limiter.clear()
    for i in range(ratelimit.login_ip_limiter.limit):
        _login(client, f"nobody-{i}", "wrong")
    r = _login(client, "nobody-x", "wrong")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_correct_password_is_refused_while_rate_limited(client, victim):
    """The whole point: exhausting the budget must stop the attacker mid-search."""
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    for i in range(ratelimit.login_ip_limiter.limit):
        _login(client, f"nobody-{i}", "wrong")
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
        # Clear the per-IP budget each round to simulate a fresh source address.
        ratelimit.login_ip_limiter.clear()
        assert _login(client, victim["username"], "wrong-password").status_code == 401
    ratelimit.login_ip_limiter.clear()
    assert _login(client, victim["username"], "wrong-password").status_code == 429


def test_successful_login_clears_the_username_failure_count(client, victim):
    """A few typos followed by the right password must not leave the user throttled."""
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    for _ in range(ratelimit.login_user_limiter.limit - 1):
        _login(client, victim["username"], "wrong-password")
    assert _login(client, victim["username"], victim["password"]).status_code == 200
    # Budget is back to full: another near-limit run of typos still is not blocked.
    for _ in range(ratelimit.login_user_limiter.limit - 1):
        assert _login(client, victim["username"], "wrong-password").status_code == 401


def test_username_counter_is_case_insensitive(client, victim):
    """Otherwise 'Admin' and 'admin' would each get their own budget."""
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    for _ in range(ratelimit.login_user_limiter.limit):
        ratelimit.login_ip_limiter.clear()
        _login(client, victim["username"].upper(), "wrong-password")
    ratelimit.login_ip_limiter.clear()
    assert _login(client, victim["username"], "wrong-password").status_code == 429


def test_rate_limited_login_is_audited(client, db, victim):
    ratelimit.login_ip_limiter.clear()
    ratelimit.login_user_limiter.clear()
    before, _ = crud.query_audit_logs(db, action="login_rate_limited")
    for i in range(ratelimit.login_ip_limiter.limit + 1):
        _login(client, f"nobody-{i}", "wrong")
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
