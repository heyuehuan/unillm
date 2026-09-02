"""
The environment-variable key path must not leak the key.

Two ways it can: comparing byte by byte tells an attacker who can time the
response how much of their guess was right, and writing the start of a rejected
key into the log puts a fragment of a secret somewhere far easier to read than
the key store.
"""

import asyncio
import logging

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy import auth


def test_a_wrong_key_is_rejected_and_the_right_one_accepted():
    allowed = {"sk-alpha", "sk-beta"}
    assert auth._matches_any_env_key("sk-beta", allowed)
    assert not auth._matches_any_env_key("sk-gamma", allowed)
    assert not auth._matches_any_env_key("sk-bet", allowed)
    assert not auth._matches_any_env_key("", allowed)


def test_comparison_never_stops_at_the_first_matching_key(monkeypatch):
    """
    Every configured key is compared, so the work done does not reveal which one
    matched or how many were checked first.
    """
    compared = []
    real = auth.hmac.compare_digest

    def counting(a, b):
        compared.append(b)
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", counting)
    allowed = {"sk-alpha", "sk-beta", "sk-gamma"}
    assert auth._matches_any_env_key("sk-alpha", allowed)
    assert sorted(compared) == sorted(allowed)


def test_the_log_tag_carries_no_part_of_the_key():
    key = "sk-supersecret-value"
    tag = auth._key_fingerprint(key)
    assert tag not in key
    assert not any(key.startswith(tag[:n]) for n in range(3, len(tag) + 1))
    # Stable, so the same key is recognisable across log lines.
    assert tag == auth._key_fingerprint(key)
    assert tag != auth._key_fingerprint(key + "x")


def test_rejecting_a_key_does_not_write_it_to_the_log(caplog, monkeypatch):
    """Drive the real auth path, not a stand-in for it."""
    monkeypatch.setenv("UNILLM_MASTER_KEY", "sk-the-real-key")
    monkeypatch.delenv("UNILLM_API_KEYS", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

    init_db()
    presented = "sk-guessed-wrong"
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=presented)
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                       "headers": [], "query_string": b""})
    db = SessionLocal()
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(HTTPException) as raised:
                asyncio.run(auth.user_api_key_auth(request, credentials, db))
        assert raised.value.status_code == 401
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)

    assert presented not in caplog.text
    assert presented[:8] not in caplog.text
    assert auth._key_fingerprint(presented) in caplog.text
