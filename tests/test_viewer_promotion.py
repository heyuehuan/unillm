"""
Promoting a viewer provisions the personal project it never had.

A viewer is created without a personal project or key, because a viewer has no
use for either. Promoting one to user or admin used to leave the account holding
a developer role with nowhere of its own to keep keys: the console's personal
project view had nothing to show and the account could never mint a key without
an admin first inviting it into some other project.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base, User
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    db = SessionLocal()
    try:
        crud.create_user(db, username="promoadmin", hashed_password=hash_password("adminpass1"),
                         global_role="admin")
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def headers(client):
    token = client.post("/api/auth/login",
                        json={"username": "promoadmin", "password": "adminpass1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer(client, headers, request):
    name = f"v{abs(hash(request.node.name)) % 10**8}"
    r = client.post("/api/users", json={"username": name, "password": "viewerpass1",
                                        "global_role": "viewer"}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["api_key"] is None
    return body["user"]


def _personal_project_id(user_id):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first().personal_project_id
    finally:
        db.close()


@pytest.mark.parametrize("role", ["user", "admin"])
def test_promotion_creates_the_personal_project_and_key(client, headers, viewer, role):
    assert _personal_project_id(viewer["id"]) is None

    r = client.put(f"/api/users/{viewer['id']}", json={"global_role": role}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["api_key"], "the new personal key is returned once, as at creation"

    project_id = _personal_project_id(viewer["id"])
    assert project_id is not None


def test_the_new_key_works_and_belongs_to_that_account_alone(client, headers, viewer):
    key = client.put(f"/api/users/{viewer['id']}", json={"global_role": "user"},
                     headers=headers).json()["api_key"]

    db = SessionLocal()
    try:
        db_key = crud.get_api_key_by_value(db, key)
        assert db_key is not None
        assert db_key.project_id == _personal_project_id(viewer["id"])
        # Standalone: nobody else is a member of it.
        members = crud.list_project_members(db, db_key.project_id)
        assert [access.user_id for access, _user in members] == [viewer["id"]]
    finally:
        db.close()


def test_a_second_update_does_not_mint_another_project(client, headers, viewer):
    first = client.put(f"/api/users/{viewer['id']}", json={"global_role": "user"},
                       headers=headers).json()
    project_id = _personal_project_id(viewer["id"])

    again = client.put(f"/api/users/{viewer['id']}", json={"global_role": "user", "name": "Renamed"},
                       headers=headers).json()
    assert again["api_key"] is None
    assert _personal_project_id(viewer["id"]) == project_id
    assert first["api_key"]


def test_demotion_to_viewer_provisions_nothing(client, headers):
    r = client.post("/api/users", json={"username": "demoteme", "password": "userpass12",
                                        "global_role": "user"}, headers=headers)
    user_id = r.json()["user"]["id"]
    project_id = _personal_project_id(user_id)
    assert project_id is not None

    r = client.put(f"/api/users/{user_id}", json={"global_role": "viewer"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["api_key"] is None
    # The project it already had is left alone — the keys in it are still the
    # account's own, and demotion is not a delete.
    assert _personal_project_id(user_id) == project_id
