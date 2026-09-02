"""
Who may reveal a project's API key.

A developer on a project already holds its keys — they are in the client config
they use every day — so reading one back through the console is not a capability
they lack. Withholding it only pushed people towards keeping private copies of
the plaintext, which is worse. Viewers are the line: they cannot see the key list
at all, so they cannot reveal either. Every reveal is written to the audit trail.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app

ACCOUNTS = {
    "rv-admin": "admin",       # global admin, creates everything
    "rv-projadmin": "user",    # project role: admin
    "rv-dev": "user",          # project role: developer
    "rv-viewer": "user",       # project role: viewer
    "rv-outsider": "user",     # not a member at all
}


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    db = SessionLocal()
    try:
        for username, role in ACCOUNTS.items():
            crud.create_user(db, username=username, hashed_password=hash_password("passwd12345"),
                             global_role=role)
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _token(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "passwd12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def tokens(client):
    return {u: _token(client, u) for u in ACCOUNTS}


@pytest.fixture(scope="module")
def user_ids(client, tokens):
    users = client.get("/api/users", headers=tokens["rv-admin"]).json()
    return {u["username"]: u["id"] for u in users}


@pytest.fixture(scope="module")
def key(client, tokens, user_ids):
    """A shared project with one key, and one member per role."""
    admin = tokens["rv-admin"]
    project_id = client.post("/api/projects", json={"name": "reveal-access"}, headers=admin).json()["id"]
    for username, role in (("rv-projadmin", "admin"), ("rv-dev", "developer"), ("rv-viewer", "viewer")):
        r = client.post(f"/api/projects/{project_id}/members",
                        json={"user_id": user_ids[username], "role": role}, headers=admin)
        assert r.status_code in (200, 201), r.text
    created = client.post(f"/api/projects/{project_id}/keys",
                          json={"name": "shared"}, headers=admin).json()
    return {"project_id": project_id, "id": created["key"]["id"], "plaintext": created["api_key"]}


def test_a_developer_can_reveal(client, tokens, key):
    r = client.get(f"/api/keys/{key['id']}/reveal", headers=tokens["rv-dev"])
    assert r.status_code == 200, r.text
    assert r.json()["api_key"] == key["plaintext"]


def test_a_project_admin_can_reveal(client, tokens, key):
    r = client.get(f"/api/keys/{key['id']}/reveal", headers=tokens["rv-projadmin"])
    assert r.status_code == 200, r.text
    assert r.json()["api_key"] == key["plaintext"]


def test_a_global_admin_can_reveal_without_being_a_member(client, tokens, key):
    r = client.get(f"/api/keys/{key['id']}/reveal", headers=tokens["rv-admin"])
    assert r.status_code == 200, r.text


def test_a_viewer_cannot_reveal(client, tokens, key):
    r = client.get(f"/api/keys/{key['id']}/reveal", headers=tokens["rv-viewer"])
    assert r.status_code == 403
    # A viewer cannot list the project's keys either, so this is consistent.
    assert client.get(f"/api/projects/{key['project_id']}/keys",
                      headers=tokens["rv-viewer"]).status_code == 403


def test_a_non_member_cannot_reveal(client, tokens, key):
    assert client.get(f"/api/keys/{key['id']}/reveal", headers=tokens["rv-outsider"]).status_code == 403


def test_a_developer_reveal_is_audited(client, tokens, key):
    client.get(f"/api/keys/{key['id']}/reveal", headers=tokens["rv-dev"])
    db = SessionLocal()
    try:
        rows, _ = crud.query_audit_logs(db, action="api_key_revealed")
        dev_id = client.get("/api/users/me", headers=tokens["rv-dev"]).json()["id"]
        mine = [r for r in rows if r.user_id == dev_id]
        assert mine, "a developer's reveal must be recorded like any other"
        assert mine[0].severity == "warning"
    finally:
        db.close()
