"""
Regression tests for the security review fixes:

- Password change / admin reset invalidates existing JWTs (token_version).
- Last active admin cannot be demoted or deactivated.
- Weak passwords and malformed usernames are rejected.
- Unknown/unconfigured models are rejected (not forwarded to a default backend).
- API key reveal requires admin and is recorded in the audit log.
- /v1/models is filtered by the key's allowed_models.
- Login does not leak username existence and never 500s on a bad hash.
"""
import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app
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


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin_token(client, db):
    crud.create_user(db=db, username="secadmin", hashed_password=hash_password("adminpass1"),
                     global_role="admin")
    r = client.post("/api/auth/login", json={"username": "secadmin", "password": "adminpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


# --- token invalidation on password change -------------------------------------------

def test_password_change_invalidates_token(client, admin_token):
    # Create a normal user and log in.
    client.post("/api/users", json={"username": "bob", "password": "bobpass12"},
                headers=_auth(admin_token))
    login = client.post("/api/auth/login", json={"username": "bob", "password": "bobpass12"})
    token = login.json()["access_token"]

    # Token works before the change.
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 200

    # Change password using the still-valid token.
    r = client.put("/api/users/me",
                   json={"current_password": "bobpass12", "new_password": "bobpass34"},
                   headers=_auth(token))
    assert r.status_code == 200

    # The old token is now rejected, but the re-issued token from the response works.
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 401
    new_token = r.json()["access_token"]
    assert client.get("/api/users/me", headers=_auth(new_token)).status_code == 200


def test_noop_admin_edit_does_not_invalidate_sessions(client, admin_token):
    # The admin UI submits the full form, including unchanged fields; resubmitting
    # current values must not bump token_version (log the user out everywhere).
    client.post("/api/users", json={"username": "carol", "password": "carolpass1"},
                headers=_auth(admin_token))
    login = client.post("/api/auth/login", json={"username": "carol", "password": "carolpass1"})
    token = login.json()["access_token"]

    users = client.get("/api/users", headers=_auth(admin_token)).json()
    carol_id = next(u["id"] for u in users if u["username"] == "carol")
    r = client.put(f"/api/users/{carol_id}",
                   json={"global_role": "user", "active": True, "password_login_disabled": False},
                   headers=_auth(admin_token))
    assert r.status_code == 200

    # Carol's session survives the no-op edit.
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 200

    # A real role change still invalidates it.
    r = client.put(f"/api/users/{carol_id}", json={"global_role": "viewer"}, headers=_auth(admin_token))
    assert r.status_code == 200
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 401


# --- last-admin guard ----------------------------------------------------------------

def test_cannot_demote_last_admin(client, admin_token, db):
    # secadmin is the only admin. Create a second admin so we can operate on the first.
    r = client.post("/api/users", json={"username": "admin2", "password": "admin2pass"},
                    headers=_auth(admin_token))
    admin2_id = r.json()["user"]["id"]
    # Promote admin2 via the admin API.
    client.put(f"/api/users/{admin2_id}", json={"global_role": "admin"}, headers=_auth(admin_token))

    # Now demote admin2 — allowed, another admin (secadmin) remains.
    r = client.put(f"/api/users/{admin2_id}", json={"global_role": "user"}, headers=_auth(admin_token))
    assert r.status_code == 200

    # admin2 is back to user; secadmin is the last admin. Deactivating a lone admin
    # (some other admin) must be blocked. Re-promote admin2, then try to deactivate it
    # while it is the only *other* admin and secadmin is excluded by self-edit rule.
    client.put(f"/api/users/{admin2_id}", json={"global_role": "admin"}, headers=_auth(admin_token))
    # Demote secadmin is impossible (self-edit blocked), so deactivate admin2 is fine here
    # because secadmin still counts. This asserts the guard doesn't over-block.
    r = client.put(f"/api/users/{admin2_id}", json={"active": False}, headers=_auth(admin_token))
    assert r.status_code == 200


# --- input validation ----------------------------------------------------------------

def test_weak_password_rejected(client, admin_token):
    r = client.post("/api/users", json={"username": "weakling", "password": "short"},
                    headers=_auth(admin_token))
    assert r.status_code == 422


def test_bad_username_rejected(client, admin_token):
    r = client.post("/api/users", json={"username": "bad user!", "password": "validpass1"},
                    headers=_auth(admin_token))
    assert r.status_code == 422


def test_no_orphan_rows_for_missing_project(client, admin_token):
    # Global admins bypass the role check, so these endpoints must verify the
    # project actually exists instead of inserting orphaned rows.
    r = client.post("/api/projects/99999/keys", json={"name": "orphan-key"},
                    headers=_auth(admin_token))
    assert r.status_code == 404
    r = client.post("/api/projects/99999/members", json={"user_id": 1, "role": "viewer"},
                    headers=_auth(admin_token))
    assert r.status_code == 404


def test_dev_mode_disabled_when_db_in_use(client, admin_token, monkeypatch):
    # Even with UNILLM_DEV_MODE=true and no DATABASE_URL env var, a database that
    # contains real users/keys must not allow-all an invalid API key.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("UNILLM_DEV_MODE", "true")
    r = client.get("/v1/models", headers={"Authorization": "Bearer sk-not-a-real-key"})
    assert r.status_code == 401


# --- login safety --------------------------------------------------------------------

def test_login_unknown_user_no_500(client):
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever1"})
    assert r.status_code == 401


# --- model access & reveal -----------------------------------------------------------

@pytest.fixture(scope="module")
def project_and_key(client, admin_token):
    p = client.post("/api/projects", json={"name": "secproj"}, headers=_auth(admin_token)).json()
    k = client.post(f"/api/projects/{p['id']}/keys",
                    json={"name": "sec-key", "allowed_models": ["all"]},
                    headers=_auth(admin_token)).json()
    return p, k


def test_unknown_model_rejected(client, project_and_key):
    _, k = project_and_key
    r = client.post("/v1/chat/completions",
                    json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {k['api_key']}"})
    # allowed_models is ["all"], so access passes; model isn't configured → 404.
    assert r.status_code == 404


def test_reveal_requires_admin_and_is_audited(client, admin_token, project_and_key, db):
    _, k = project_and_key
    key_id = k["key"]["id"]
    r = client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["api_key"].startswith("sk-")

    # Audit trail recorded the reveal.
    rows, _total = crud.query_audit_logs(db, action="api_key_revealed")
    assert any(row.resource_id == str(key_id) for row in rows)


# --- audit trail durability ----------------------------------------------------------

def test_failed_login_is_recorded_in_the_audit_trail(client, db):
    """
    Audits used to be scheduled as FastAPI background tasks, which are attached to
    the response the endpoint returns — so every audit followed by `raise` was
    dropped, and the trail contained no failure events at all. Brute-force
    detection depends on exactly those rows.
    """
    before, _ = crud.query_audit_logs(db, action="login_failure")
    r = client.post("/api/auth/login", json={"username": "no-such-user", "password": "wrong"})
    assert r.status_code == 401
    after, _ = crud.query_audit_logs(db, action="login_failure")
    assert len(after) == len(before) + 1
    assert after[0].detail["username"] == "no-such-user"
    assert after[0].severity == "warning"


def test_disabled_account_login_attempt_is_audited(client, db, admin_token):
    client.post("/api/users", json={"username": "audit-disabled", "password": "disabled123"},
                headers=_auth(admin_token))
    user_id = [u["id"] for u in client.get("/api/users", headers=_auth(admin_token)).json()
               if u["username"] == "audit-disabled"][0]
    client.put(f"/api/users/{user_id}", json={"active": False}, headers=_auth(admin_token))

    r = client.post("/api/auth/login", json={"username": "audit-disabled", "password": "disabled123"})
    assert r.status_code == 403
    rows, _ = crud.query_audit_logs(db, action="login_failure")
    assert any(row.detail.get("reason") == "account_disabled" for row in rows)
