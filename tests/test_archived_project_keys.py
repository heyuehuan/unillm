"""
An archived project cannot mint new API keys.

Authentication refuses every request carrying an archived project's key, so a key
created there would fail at its first use and the owner would find out only then.
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
        crud.create_user(db, username="keyadmin", hashed_password=hash_password("adminpass1"),
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
                        json={"username": "keyadmin", "password": "adminpass1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def project(client, headers, request):
    # Project names are unique, so each test gets its own.
    r = client.post("/api/projects", json={"name": f"key project for {request.node.name}"},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _archive(client, headers, project_id, archived=True):
    r = client.put(f"/api/projects/{project_id}", json={"archived": archived}, headers=headers)
    assert r.status_code == 200, r.text


def test_creating_a_key_on_an_archived_project_is_refused(client, headers, project):
    _archive(client, headers, project)
    r = client.post(f"/api/projects/{project}/keys", json={"name": "doomed"}, headers=headers)
    assert r.status_code == 400
    assert "archived" in r.json()["detail"].lower()


def test_restoring_the_project_makes_key_creation_work_again(client, headers, project):
    _archive(client, headers, project)
    _archive(client, headers, project, archived=False)
    r = client.post(f"/api/projects/{project}/keys", json={"name": "fine now"}, headers=headers)
    assert r.status_code == 201, r.text


def test_an_active_project_is_unaffected(client, headers, project):
    r = client.post(f"/api/projects/{project}/keys", json={"name": "ordinary"}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["api_key"].startswith("sk-")


def test_existing_keys_are_still_listed_after_archiving(client, headers, project):
    client.post(f"/api/projects/{project}/keys", json={"name": "made earlier"}, headers=headers)
    _archive(client, headers, project)
    names = [k["name"] for k in client.get(f"/api/projects/{project}/keys", headers=headers).json()]
    assert "made earlier" in names
