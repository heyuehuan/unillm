"""
Covers project membership management:

- GET /projects/{id}/member-candidates (project admins may add members without
  being global admins, and the list excludes members and disabled accounts)
- POST /projects/{id}/members rejecting disabled accounts with a clear message
- PUT /projects/{id}/members/{user_id} working for a disabled member
- members carrying the account's active flag
"""
import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app
from unillm.db.database import init_db, engine, SessionLocal
from unillm.db.models import Base
from unillm.db import crud
from unillm.proxy.api_routes import hash_password


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


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
    crud.create_user(db=db, username="padmin-root", hashed_password=hash_password("adminpass"),
                     global_role="admin")
    r = client.post("/api/auth/login", json={"username": "padmin-root", "password": "adminpass"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _make_user(client, admin_token, username, password="userpass123"):
    r = client.post("/api/users", headers=_auth(admin_token),
                    json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["user"]


def _login(client, username, password="userpass123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _make_project(client, admin_token, name):
    r = client.post("/api/projects", headers=_auth(admin_token), json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _add_member(client, token, project_id, user_id, role):
    return client.post(f"/api/projects/{project_id}/members", headers=_auth(token),
                       json={"user_id": user_id, "role": role})


# ── member candidates ──────────────────────────────────────

def test_project_admin_can_list_candidates_and_add_member(client, admin_token):
    project = _make_project(client, admin_token, "cand-project")
    lead = _make_user(client, admin_token, "cand-lead")
    hire = _make_user(client, admin_token, "cand-hire")
    assert _add_member(client, admin_token, project["id"], lead["id"], "admin").status_code == 201

    lead_token = _login(client, "cand-lead")
    # A project admin is not a global admin: /api/users stays closed to them...
    assert client.get("/api/users", headers=_auth(lead_token)).status_code == 403
    # ...but the candidate list is what the add-member form needs.
    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(lead_token))
    assert r.status_code == 200, r.text
    names = [u["username"] for u in r.json()]
    assert "cand-hire" in names
    assert "cand-lead" not in names  # already a member

    r = _add_member(client, lead_token, project["id"], hire["id"], "developer")
    assert r.status_code == 201, r.text

    # the freshly added member drops out of the candidate list
    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(lead_token))
    assert "cand-hire" not in [u["username"] for u in r.json()]


def test_candidates_exclude_disabled_accounts(client, admin_token):
    project = _make_project(client, admin_token, "cand-disabled-project")
    user = _make_user(client, admin_token, "cand-disabled")

    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(admin_token))
    assert "cand-disabled" in [u["username"] for u in r.json()]

    assert client.put(f"/api/users/{user['id']}", headers=_auth(admin_token),
                      json={"active": False}).status_code == 200

    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(admin_token))
    assert "cand-disabled" not in [u["username"] for u in r.json()]


def test_candidates_require_project_admin(client, admin_token):
    project = _make_project(client, admin_token, "cand-perm-project")
    dev = _make_user(client, admin_token, "cand-dev")
    assert _add_member(client, admin_token, project["id"], dev["id"], "developer").status_code == 201

    dev_token = _login(client, "cand-dev")
    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(dev_token))
    assert r.status_code == 403

    r = client.get("/api/projects/99999/member-candidates", headers=_auth(admin_token))
    assert r.status_code == 404


# ── disabled accounts as members ───────────────────────────

def test_adding_a_disabled_user_explains_why(client, admin_token):
    project = _make_project(client, admin_token, "disabled-add-project")
    user = _make_user(client, admin_token, "disabled-add")
    assert client.put(f"/api/users/{user['id']}", headers=_auth(admin_token),
                      json={"active": False}).status_code == 200

    r = _add_member(client, admin_token, project["id"], user["id"], "developer")
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]

    r = _add_member(client, admin_token, project["id"], 99999, "developer")
    assert r.status_code == 404


def test_role_update_works_for_a_disabled_member(client, admin_token):
    project = _make_project(client, admin_token, "disabled-role-project")
    user = _make_user(client, admin_token, "disabled-role")
    assert _add_member(client, admin_token, project["id"], user["id"], "developer").status_code == 201
    assert client.put(f"/api/users/{user['id']}", headers=_auth(admin_token),
                      json={"active": False}).status_code == 200

    r = client.put(f"/api/projects/{project['id']}/members/{user['id']}",
                   headers=_auth(admin_token), json={"role": "admin"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert r.json()["active"] is False

    members = client.get(f"/api/projects/{project['id']}/members", headers=_auth(admin_token)).json()
    disabled = next(m for m in members if m["username"] == "disabled-role")
    assert disabled["role"] == "admin"
    assert disabled["active"] is False


def test_members_report_active_accounts(client, admin_token):
    project = _make_project(client, admin_token, "active-flag-project")
    user = _make_user(client, admin_token, "active-flag")
    assert _add_member(client, admin_token, project["id"], user["id"], "viewer").status_code == 201

    members = client.get(f"/api/projects/{project['id']}/members", headers=_auth(admin_token)).json()
    assert all(m["active"] for m in members)
