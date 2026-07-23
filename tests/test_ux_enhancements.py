"""
Covers the UX-enhancement API changes:

- PUT /users/me profile editing (name/email, with and without password change)
- PUT /projects/{id} rename / archive, and archived keys being rejected at the proxy
- PUT /keys/{id} editing name / allowed_models with permission checks
- GET /logs `mine` scope and the ssh_username restriction for non-admins
- GET /projects returning member/key counts and hiding archived projects
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


@pytest.fixture(scope="module")
def admin_token(client, db):
    crud.create_user(db=db, username="admin", hashed_password=hash_password("adminpass"),
                     global_role="admin")
    r = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def user_token(client, admin_token):
    r = client.post("/api/users", headers=_auth(admin_token),
                    json={"username": "dev1", "password": "devpass123"})
    assert r.status_code == 201
    r = client.post("/api/auth/login", json={"username": "dev1", "password": "devpass123"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# PUT /users/me — profile editing
# ---------------------------------------------------------------------------

def test_update_me_profile_only(client, user_token):
    r = client.put("/api/users/me", headers=_auth(user_token),
                   json={"name": "Dev One", "email": "dev1@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["name"] == "Dev One"
    assert body["user"]["email"] == "dev1@example.com"
    # Profile-only changes must not rotate the token
    assert body["access_token"] is None
    # ... and the old token keeps working
    r = client.get("/api/users/me", headers=_auth(user_token))
    assert r.status_code == 200


def test_update_me_duplicate_email_409(client, admin_token, user_token):
    r = client.put("/api/users/me", headers=_auth(admin_token),
                   json={"email": "admin@example.com"})
    assert r.status_code == 200
    r = client.put("/api/users/me", headers=_auth(user_token),
                   json={"email": "admin@example.com"})
    assert r.status_code == 409


def test_update_me_password_requires_current(client, user_token):
    r = client.put("/api/users/me", headers=_auth(user_token),
                   json={"new_password": "anotherpass1"})
    assert r.status_code == 400


def test_update_me_password_change_returns_token(client, admin_token):
    # Dedicated user so the shared user_token fixture stays valid for later tests.
    r = client.post("/api/users", headers=_auth(admin_token),
                    json={"username": "pwuser", "password": "pwpass1234"})
    assert r.status_code == 201
    token = client.post("/api/auth/login",
                        json={"username": "pwuser", "password": "pwpass1234"}).json()["access_token"]
    r = client.put("/api/users/me", headers=_auth(token),
                   json={"current_password": "pwpass1234", "new_password": "pwpass5678"})
    assert r.status_code == 200
    new_token = r.json()["access_token"]
    assert new_token
    # Old token invalidated, new one valid
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 401
    assert client.get("/api/users/me", headers=_auth(new_token)).status_code == 200


# ---------------------------------------------------------------------------
# Projects — edit / archive
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project(client, admin_token):
    r = client.post("/api/projects", headers=_auth(admin_token),
                    json={"name": "proj-a", "description": "first"})
    assert r.status_code == 201
    return r.json()


def test_update_project_rename(client, admin_token, project):
    r = client.put(f"/api/projects/{project['id']}", headers=_auth(admin_token),
                   json={"name": "proj-a-renamed", "description": "second"})
    assert r.status_code == 200
    assert r.json()["name"] == "proj-a-renamed"
    assert r.json()["description"] == "second"


def test_update_project_rename_conflict(client, admin_token, project):
    r = client.post("/api/projects", headers=_auth(admin_token), json={"name": "proj-b"})
    assert r.status_code == 201
    r = client.put(f"/api/projects/{project['id']}", headers=_auth(admin_token),
                   json={"name": "proj-b"})
    assert r.status_code == 409


def test_update_project_requires_admin(client, user_token, project):
    r = client.put(f"/api/projects/{project['id']}", headers=_auth(user_token),
                   json={"name": "hijack"})
    assert r.status_code == 403


def test_archive_blocks_keys_and_hides_project(client, admin_token, project):
    pid = project["id"]
    r = client.post(f"/api/projects/{pid}/keys", headers=_auth(admin_token),
                    json={"name": "archived-key"})
    assert r.status_code == 201
    plaintext = r.json()["api_key"]

    r = client.put(f"/api/projects/{pid}", headers=_auth(admin_token), json={"archived": True})
    assert r.status_code == 200
    assert r.json()["archived"] is True

    # Key from an archived project is rejected by proxy auth
    r = client.post("/v1/chat/completions", headers=_auth(plaintext),
                    json={"model": "whatever", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403

    # Archived project hidden from the default list, visible with include_archived
    names = [p["name"] for p in client.get("/api/projects", headers=_auth(admin_token)).json()]
    assert "proj-a-renamed" not in names
    names = [p["name"] for p in client.get(
        "/api/projects?include_archived=true", headers=_auth(admin_token)).json()]
    assert "proj-a-renamed" in names

    # Unarchive restores the key
    r = client.put(f"/api/projects/{pid}", headers=_auth(admin_token), json={"archived": False})
    assert r.status_code == 200
    r = client.post("/v1/chat/completions", headers=_auth(plaintext),
                    json={"model": "whatever", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code != 403


def test_project_list_includes_counts(client, admin_token):
    projects = client.get("/api/projects", headers=_auth(admin_token)).json()
    assert projects
    for p in projects:
        assert p["member_count"] is not None
        assert p["key_count"] is not None


# ---------------------------------------------------------------------------
# API key editing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_key(client, admin_token, project):
    r = client.post(f"/api/projects/{project['id']}/keys", headers=_auth(admin_token),
                    json={"name": "editable-key", "allowed_models": ["model-a"]})
    assert r.status_code == 201
    return r.json()["key"]


def test_update_key_name_and_models(client, admin_token, api_key):
    r = client.put(f"/api/keys/{api_key['id']}", headers=_auth(admin_token),
                   json={"name": "renamed-key", "allowed_models": ["model-a", "model-b"]})
    assert r.status_code == 200
    assert r.json()["name"] == "renamed-key"
    assert r.json()["allowed_models"] == ["model-a", "model-b"]


def test_update_key_null_models_means_all(client, admin_token, api_key):
    r = client.put(f"/api/keys/{api_key['id']}", headers=_auth(admin_token),
                   json={"allowed_models": None})
    assert r.status_code == 200
    assert r.json()["allowed_models"] == ["all"]


def test_update_key_requires_project_admin(client, user_token, api_key):
    r = client.put(f"/api/keys/{api_key['id']}", headers=_auth(user_token),
                   json={"name": "nope"})
    assert r.status_code == 403


def test_update_revoked_key_rejected(client, admin_token, project):
    r = client.post(f"/api/projects/{project['id']}/keys", headers=_auth(admin_token),
                    json={"name": "to-revoke"})
    key_id = r.json()["key"]["id"]
    assert client.delete(f"/api/keys/{key_id}", headers=_auth(admin_token)).status_code == 204
    r = client.put(f"/api/keys/{key_id}", headers=_auth(admin_token), json={"name": "zombie"})
    assert r.status_code == 400


def test_list_keys_includes_revoked(client, admin_token, project):
    keys = client.get(f"/api/projects/{project['id']}/keys", headers=_auth(admin_token)).json()
    assert any(not k["active"] for k in keys)
    assert any(k["active"] for k in keys)


# ---------------------------------------------------------------------------
# Logs — mine scope and ssh_username restriction
# ---------------------------------------------------------------------------

def test_mine_scope_filters_to_own_ssh_username(client, user_token, db):
    crud.create_request_log(db=db, model="m1", ssh_username="dev1", status_code=200)
    crud.create_request_log(db=db, model="m1", ssh_username="someone-else", status_code=200)
    db.commit()
    r = client.get("/api/logs/requests?mine=true", headers=_auth(user_token))
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "own SSH-signed request (no project) must be visible in mine scope"
    assert all(i["ssh_username"] == "dev1" for i in items)


def test_non_admin_cannot_query_other_ssh_username(client, user_token):
    r = client.get("/api/logs/requests?ssh_username=someone-else", headers=_auth(user_token))
    assert r.status_code == 403
    r = client.get("/api/logs/stats?ssh_username=someone-else", headers=_auth(user_token))
    assert r.status_code == 403


def test_admin_can_query_any_ssh_username(client, admin_token):
    r = client.get("/api/logs/requests?ssh_username=someone-else", headers=_auth(admin_token))
    assert r.status_code == 200
    assert all(i["ssh_username"] == "someone-else" for i in r.json()["items"])


def test_mine_stats_scope(client, user_token):
    r = client.get("/api/logs/stats?mine=true", headers=_auth(user_token))
    assert r.status_code == 200
    assert r.json()["total_requests"] >= 1
