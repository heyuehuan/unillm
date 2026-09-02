import os

import pytest

# In-memory SQLite with StaticPool — fast, isolated, no file cleanup needed
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["UNILLM_JWT_SECRET"] = "test-secret"


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """
    Rate-limit state is process-global, so without this one test's login attempts
    would count against the next one's budget and cause order-dependent 429s.
    Tests that exercise the limiter deliberately still start from a clean slate.
    """
    from unillm.proxy import ratelimit

    for limiter in (ratelimit.login_attempt_limiter, ratelimit.login_ip_limiter,
                    ratelimit.login_user_limiter, ratelimit.docs_limiter,
                    ratelimit.login_audit_limiter, ratelimit.profile_email_limiter,
                    ratelimit.adk_action_limiter):
        limiter.clear()
    yield
