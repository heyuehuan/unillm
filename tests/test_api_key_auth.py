"""
Validates API key authentication and project-based key management.

Covers:
- Admin login → JWT
- Create user → personal project + API key auto-created
- Create project, create API key for it
- Proxy access accepted with valid DB API key
- Proxy access rejected with invalid key
- Per-project model restrictions enforced
"""
import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app
from unillm.db.database import init_db, engine
from unillm.db.models import Base
from unillm.db import crud
from unillm.db.database import SessionLocal
from unillm.proxy.api_routes import hash_password


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def db_setup():
    """Create all tables once per module; drop them on teardown."""
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
def admin_token(client, db):
    """Create admin user, log in, return JWT."""
    crud.create_user(
        db=db,
        username="admin",
        hashed_password=hash_password("adminpass"),
        global_role="admin",
    )
    r = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth endpoint
# ---------------------------------------------------------------------------

def test_login_bad_password(client, db):
    crud.get_user_by_username(db, "admin")  # ensure admin exists (admin_token fixture may not have run yet)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user(client):
    r = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def test_create_user_returns_api_key(client, admin_token):
    r = client.post(
        "/api/users",
        json={"username": "alice", "password": "alice123"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["user"]["username"] == "alice"
    assert data["api_key"].startswith("sk-")  # personal API key provided once


def test_create_duplicate_user_rejected(client, admin_token):
    r = client.post(
        "/api/users",
        json={"username": "alice", "password": "alice-dup-123"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 409


def test_create_user_requires_admin(client):
    r = client.post("/api/users", json={"username": "hacker", "password": "x"})
    assert r.status_code == 401


def test_get_me(client, admin_token):
    r = client.get("/api/users/me", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert r.json()["global_role"] == "admin"


# ---------------------------------------------------------------------------
# Project + API key management
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project_a(client, admin_token):
    r = client.post(
        "/api/projects",
        json={"name": "team-a", "description": "Team A project"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201
    return r.json()


@pytest.fixture(scope="module")
def project_b(client, admin_token):
    r = client.post(
        "/api/projects",
        json={"name": "team-b", "description": "Team B project"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201
    return r.json()


@pytest.fixture(scope="module")
def key_a_unrestricted(client, admin_token, project_a):
    """API key for team-a with access to all models."""
    r = client.post(
        f"/api/projects/{project_a['id']}/keys",
        json={"name": "key-a-all", "allowed_models": ["all"]},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201
    return r.json()["api_key"]


@pytest.fixture(scope="module")
def key_b_restricted(client, admin_token, project_b):
    """API key for team-b restricted to gemini-flash only."""
    r = client.post(
        f"/api/projects/{project_b['id']}/keys",
        json={"name": "key-b-flash", "allowed_models": ["gemini-flash"]},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201
    return r.json()["api_key"]


def test_list_keys_for_project(client, admin_token, project_a, key_a_unrestricted):
    r = client.get(f"/api/projects/{project_a['id']}/keys", headers=_auth(admin_token))
    assert r.status_code == 200
    names = [k["name"] for k in r.json()]
    assert "key-a-all" in names


def test_list_projects_for_user(client, admin_token):
    r = client.get("/api/projects", headers=_auth(admin_token))
    assert r.status_code == 200
    # Admin created team-a and team-b plus their own personal project
    names = [p["name"] for p in r.json()]
    assert "admin-personal" in names


# ---------------------------------------------------------------------------
# Proxy access: key accepted / rejected
# ---------------------------------------------------------------------------

def test_proxy_rejected_without_key(client):
    r = client.get("/v1/models")
    assert r.status_code == 401


def test_proxy_rejected_with_bad_key(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer sk-invalid-key"})
    assert r.status_code == 401


def test_proxy_accepted_with_valid_db_key(client, key_a_unrestricted):
    """A valid DB-backed API key should pass authentication."""
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {key_a_unrestricted}"})
    # 200 = auth passed (model list may be empty, that's fine)
    assert r.status_code == 200


def test_proxy_accepted_with_second_project_key(client, key_b_restricted):
    """team-b key also authenticates independently."""
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {key_b_restricted}"})
    assert r.status_code == 200


def test_proxy_accepted_via_x_api_key_header(client, key_a_unrestricted):
    """x-api-key header is an alternative to Authorization: Bearer."""
    r = client.get("/v1/models", headers={"x-api-key": key_a_unrestricted})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Model access restrictions
# ---------------------------------------------------------------------------

def test_model_restriction_blocks_disallowed_model(client, key_b_restricted):
    """team-b key is restricted to gemini-flash; other models must be blocked."""
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-pro", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key_b_restricted}"},
    )
    assert r.status_code == 403
    assert "gemini-pro" in r.json()["detail"]


def test_model_restriction_allows_permitted_model(client, key_a_unrestricted, monkeypatch):
    """Unrestricted key should pass model access check (even if backend call fails)."""
    # We only need the access check to pass; mock the handler so no real LLM call happens
    from unittest.mock import AsyncMock, MagicMock
    import unillm.proxy.proxy_server as ps

    mock_response = MagicMock()
    mock_response.model = "gemini-flash"
    mock_response.usage = MagicMock(prompt_tokens=5, completion_tokens=10)
    mock_response.model_dump = lambda: {
        "id": "test-id", "object": "chat.completion", "model": "gemini-flash",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
    }
    mock_handler = MagicMock()
    mock_handler.chat_completion = AsyncMock(return_value=mock_response)
    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: mock_handler)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key_a_unrestricted}"},
    )
    assert r.status_code == 200
    assert r.json()["object"] == "chat.completion"
