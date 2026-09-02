"""
A project admin cannot change or remove their own membership.

Demoting or removing yourself takes away the right you just used, and on a
project with one admin it leaves nobody who can manage members at all.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    db = SessionLocal()
    try:
        crud.create_user(db, username="selfadmin", hashed_password=hash_password("adminpass1"),
                         global_role="admin")
        crud.create_user(db, username="selfmate", hashed_password=hash_password("matepass1"),
                         global_role="user")
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    token = client.post("/api/auth/login",
                        json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def headers(client):
    return _login(client, "selfadmin", "adminpass1")


@pytest.fixture(scope="module")
def me(client, headers):
    return client.get("/api/users/me", headers=headers).json()["id"]


@pytest.fixture(scope="module")
def mate(client, headers):
    return [u["id"] for u in client.get("/api/users", headers=headers).json()
            if u["username"] == "selfmate"][0]


@pytest.fixture
def project(client, headers, me, request):
    r = client.post("/api/projects", json={"name": f"self guard {request.node.name}"},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    project_id = r.json()["id"]
    # The creator is not automatically a member, so add the admin explicitly.
    client.post(f"/api/projects/{project_id}/members", json={"user_id": me, "role": "admin"},
                headers=headers)
    return project_id


def test_admin_cannot_demote_themselves(client, headers, project, me):
    r = client.put(f"/api/projects/{project}/members/{me}", json={"role": "viewer"}, headers=headers)
    assert r.status_code == 400
    assert "own membership" in r.json()["detail"]
    roles = {m["user_id"]: m["role"]
             for m in client.get(f"/api/projects/{project}/members", headers=headers).json()}
    assert roles[me] == "admin"


def test_admin_cannot_remove_themselves(client, headers, project, me):
    r = client.delete(f"/api/projects/{project}/members/{me}", headers=headers)
    assert r.status_code == 400
    assert "own membership" in r.json()["detail"]
    ids = [m["user_id"] for m in client.get(f"/api/projects/{project}/members", headers=headers).json()]
    assert me in ids


def test_another_member_can_still_be_changed_and_removed(client, headers, project, mate):
    assert client.post(f"/api/projects/{project}/members",
                       json={"user_id": mate, "role": "developer"},
                       headers=headers).status_code in (200, 201)
    r = client.put(f"/api/projects/{project}/members/{mate}", json={"role": "viewer"}, headers=headers)
    assert r.status_code == 200, r.text
    assert client.delete(f"/api/projects/{project}/members/{mate}", headers=headers).status_code == 204
