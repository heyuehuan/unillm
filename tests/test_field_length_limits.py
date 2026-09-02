"""
Free-text request fields are bounded.

Without a cap, a single authenticated call can push megabytes into a column meant
for a name — memory to parse, disk to keep, and a console that has to render it
from then on. The limits are generous; these tests pin that they exist at all.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.db import crud


HUGE = "x" * 100_000


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    db = SessionLocal()
    try:
        crud.create_user(db, username="capadmin", hashed_password=hash_password("adminpass123"),
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
def token(client):
    r = client.post("/api/auth/login", json={"username": "capadmin", "password": "adminpass123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_rejects_an_oversized_username_or_password(client):
    for payload in ({"username": HUGE, "password": "x"}, {"username": "capadmin", "password": HUGE}):
        assert client.post("/api/auth/login", json=payload).status_code == 422


@pytest.mark.parametrize("field", ["name", "description"])
def test_project_text_fields_are_bounded(client, headers, field):
    body = {"name": "sized", field: HUGE}
    assert client.post("/api/projects", json=body, headers=headers).status_code == 422


def test_creating_a_user_rejects_an_oversized_name_or_email(client, headers):
    base = {"username": "sizeduser", "password": "password123"}
    for field in ("name", "email"):
        assert client.post("/api/users", json={**base, field: HUGE}, headers=headers).status_code == 422


def test_ssh_key_fields_are_bounded(client, headers):
    body = {"key_name": "capadmin--1", "public_key": HUGE}
    assert client.post("/api/ssh-keys", json=body, headers=headers).status_code == 422
    body = {"key_name": HUGE, "public_key": "ssh-ed25519 AAAA test"}
    assert client.post("/api/ssh-keys", json=body, headers=headers).status_code == 422


def test_the_ssh_validation_challenge_is_bounded(client, headers):
    r = client.post("/api/ssh-keys/validate", json={"signed_key": HUGE}, headers=headers)
    assert r.status_code == 422


def test_normal_sized_values_still_go_through(client, headers):
    r = client.post("/api/projects", json={"name": "a real project", "description": "x" * 500},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
