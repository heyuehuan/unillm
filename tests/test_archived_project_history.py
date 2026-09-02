"""
Archiving a project must not erase its members' own history.

Archiving takes a project off the active list and stops its keys working. It does
not revoke membership, so the usage a member already generated has to stay visible
to them — an admin could see it either way, and the member could not.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base, RequestLog
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    """An ordinary member of a project that then gets archived, plus one logged request."""
    db = SessionLocal()
    try:
        crud.create_user(db, username="archadmin", hashed_password=hash_password("adminpass1"),
                         global_role="admin")
        member, _ = crud.create_user(db, username="archmember",
                                     hashed_password=hash_password("memberpass1"))
        project = crud.create_project(db, name="to be archived", creator_id=member.id)
        db.add(RequestLog(request_id="req-archived-1", project_id=project.id,
                          model="gemini-2.0-flash", status_code=200,
                          prompt_tokens=1, completion_tokens=1, total_tokens=2))
        db.commit()
        project_id = project.id
    finally:
        db.close()

    admin = client.post("/api/auth/login",
                        json={"username": "archadmin", "password": "adminpass1"}).json()["access_token"]
    member_token = client.post("/api/auth/login",
                               json={"username": "archmember", "password": "memberpass1"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {member_token}"}
    admin_headers = {"Authorization": f"Bearer {admin}"}

    r = client.put(f"/api/projects/{project_id}", json={"archived": True}, headers=admin_headers)
    assert r.status_code == 200, r.text
    return project_id, headers, admin_headers


def _ids(response):
    return [item["request_id"] for item in response.json()["items"]]


def test_a_member_still_sees_their_archived_projects_requests(client, world):
    project_id, headers, _ = world
    assert "req-archived-1" in _ids(client.get("/api/logs/requests", headers=headers))


def test_a_member_can_filter_by_the_archived_project_explicitly(client, world):
    project_id, headers, _ = world
    r = client.get("/api/logs/requests", params={"project_ids": [project_id]}, headers=headers)
    assert _ids(r) == ["req-archived-1"]


def test_the_member_and_the_admin_see_the_same_history(client, world):
    project_id, headers, admin_headers = world
    params = {"project_ids": [project_id]}
    assert _ids(client.get("/api/logs/requests", params=params, headers=headers)) == \
           _ids(client.get("/api/logs/requests", params=params, headers=admin_headers))


def test_stats_cover_the_archived_project_too(client, world):
    project_id, headers, _ = world
    stats = client.get("/api/logs/stats", params={"project_ids": [project_id]}, headers=headers).json()
    assert stats["total_requests"] == 1


def test_the_archived_project_is_still_hidden_from_the_active_list(client, world):
    project_id, headers, _ = world
    listed = [p["id"] for p in client.get("/api/projects", headers=headers).json()]
    assert project_id not in listed
