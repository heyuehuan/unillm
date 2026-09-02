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


# ── personal projects are standalone ───────────────────────

def _personal_project(client, token):
    """The caller's own personal project, as the console would find it."""
    r = client.get("/api/projects", headers=_auth(token))
    assert r.status_code == 200, r.text
    personal = [p for p in r.json() if p["personal"]]
    assert len(personal) == 1, personal
    return personal[0]


def test_an_ordinary_user_cannot_enumerate_the_directory_through_their_personal_project(
        client, admin_token):
    """
    The leak: creating an account also creates a personal project and makes the
    account its admin, so *everyone* passed the project-admin check on that one
    project. Pointing the candidate endpoint at it answered "who else could join"
    with the whole active-user directory — the listing GET /api/users restricts to
    global admins.
    """
    _make_user(client, admin_token, "pp-snooper")
    _make_user(client, admin_token, "pp-victim")
    token = _login(client, "pp-snooper")

    project = _personal_project(client, token)
    assert client.get("/api/users", headers=_auth(token)).status_code == 403

    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(token))
    assert r.status_code == 400, r.text
    assert "personal project" in r.json()["detail"]


def test_personal_projects_reject_every_membership_change(client, admin_token):
    """Even a global admin cannot turn somebody's personal project into a shared one."""
    outsider = _make_user(client, admin_token, "pp-outsider")
    owner = _make_user(client, admin_token, "pp-owner")
    owner_token = _login(client, "pp-owner")
    project = _personal_project(client, owner_token)

    for token in (owner_token, admin_token):
        r = _add_member(client, token, project["id"], outsider["id"], "developer")
        assert r.status_code == 400, r.text

        r = client.put(f"/api/projects/{project['id']}/members/{owner['id']}",
                       headers=_auth(token), json={"role": "viewer"})
        assert r.status_code == 400, r.text

        # Also stops an owner removing themselves from the project holding their keys.
        r = client.delete(f"/api/projects/{project['id']}/members/{owner['id']}",
                          headers=_auth(token))
        assert r.status_code == 400, r.text

    members = client.get(f"/api/projects/{project['id']}/members",
                         headers=_auth(owner_token)).json()
    assert [m["username"] for m in members] == ["pp-owner"]


def test_shared_projects_stay_manageable_and_candidates_omit_contact_details(
        client, admin_token):
    project = _make_project(client, admin_token, "pp-shared-project")
    _make_user(client, admin_token, "pp-candidate")

    detail = client.get(f"/api/projects/{project['id']}", headers=_auth(admin_token)).json()
    assert detail["personal"] is False

    r = client.get(f"/api/projects/{project['id']}/member-candidates", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    entry = next(u for u in r.json() if u["username"] == "pp-candidate")
    # An identifier is enough to pick someone; email addresses are not this
    # endpoint's to hand out.
    assert set(entry) == {"id", "username"}
