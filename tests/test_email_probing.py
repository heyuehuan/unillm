"""
Changing your own email must not become a way to enumerate accounts.

The address column is unique, so saving one that someone else holds returns a 409.
That answer is honest and has to stay, so what is bounded instead is how often a
single account may ask the question.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy import ratelimit
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    db = SessionLocal()
    try:
        crud.create_user(db, username="prober", hashed_password=hash_password("proberpass1"),
                         email="prober@example.com")
        crud.create_user(db, username="target", hashed_password=hash_password("targetpass1"),
                         email="target@example.com")
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def headers(client):
    r = client.post("/api/auth/login", json={"username": "prober", "password": "proberpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_taking_someone_elses_address_is_still_refused(client, headers):
    r = client.put("/api/users/me", json={"email": "target@example.com"}, headers=headers)
    assert r.status_code == 409


def test_repeated_probing_is_throttled(client, headers):
    """
    The limiter allows a handful of changes an hour, which no real person exceeds,
    and then stops answering — so guessing addresses one at a time gets nowhere.
    """
    statuses = [
        client.put("/api/users/me", json={"email": f"guess{i}@example.com"}, headers=headers).status_code
        for i in range(8)
    ]
    assert 429 in statuses, statuses
    assert statuses.count(429) >= 2


def test_a_throttled_change_says_when_to_come_back(client, headers):
    for i in range(8):
        r = client.put("/api/users/me", json={"email": f"again{i}@example.com"}, headers=headers)
        if r.status_code == 429:
            assert int(r.headers["Retry-After"]) >= 1
            return
    pytest.fail("expected the limiter to reject one of these")


def test_changes_that_are_not_email_changes_are_not_throttled(client, headers):
    ratelimit.profile_email_limiter.clear()
    for i in range(20):
        r = client.put("/api/users/me", json={"name": f"Prober {i}"}, headers=headers)
        assert r.status_code == 200, r.text


def test_saving_the_address_you_already_have_costs_nothing(client, headers):
    ratelimit.profile_email_limiter.clear()
    current = client.get("/api/users/me", headers=headers).json()["email"]
    for _ in range(20):
        assert client.put("/api/users/me", json={"email": current}, headers=headers).status_code == 200
