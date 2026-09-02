"""
A database key with no model allowlist must not become an unrestricted key.

None means "no list to consult" and is correct for a key configured in the
environment. For a key read out of the database it means the list went missing,
and a missing allowlist has to deny rather than permit.
"""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy import auth
from unillm.proxy.api_routes import hash_password


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _authenticate(key: str, db):
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                       "headers": [], "query_string": b""})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
    return asyncio.run(auth.user_api_key_auth(request, credentials, db))


def test_a_key_whose_allowlist_went_missing_permits_nothing(db, monkeypatch):
    user, plaintext = crud.create_user(
        db, username="widening", hashed_password=hash_password("password123"))
    assert plaintext
    db_key = crud.get_api_key_by_value(db, plaintext)
    monkeypatch.setattr(type(db_key), "allowed_models", property(lambda self: None))

    result = _authenticate(plaintext, db)
    assert result.allowed_models == []
    assert not auth._check_model_access(result.allowed_models, "gemini-2.0-flash")


def test_an_ordinary_key_still_allows_its_models(db):
    user, plaintext = crud.create_user(
        db, username="unaffected", hashed_password=hash_password("password123"))
    result = _authenticate(plaintext, db)
    assert result.allowed_models == ["all"]
    assert auth._check_model_access(result.allowed_models, "gemini-2.0-flash")


def test_an_environment_key_is_still_unrestricted(db, monkeypatch):
    monkeypatch.setenv("UNILLM_MASTER_KEY", "sk-env-master")
    monkeypatch.delenv("UNILLM_API_KEYS", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    result = _authenticate("sk-env-master", db)
    assert result.allowed_models is None
    assert auth._check_model_access(result.allowed_models, "anything-at-all")
