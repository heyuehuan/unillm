"""
Timestamps that name the instant they happened.

Everything is stored naive-UTC, which is right on disk and wrong in JSON:
"2026-09-02T13:19:54" identifies no moment, so a browser applies its own offset to
it. In Toronto that placed every event four hours in the future, and the relative
formatter clamped anything in the future to "just now" — so the Models page showed
"Last success just now" and "Last fail just now" side by side, for requests hours
apart. The display was wrong in a way that looked plausible, which is the reason it
survived.

Two things are tested here, because two separate things were broken:

  * the API must state the offset, so any client resolves the same instant;
  * the deployment, not the browser, picks the zone those instants are shown in.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from unillm import timefmt
from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
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
def auth(client):
    db = SessionLocal()
    try:
        if crud.get_user_by_username(db, "tzadmin") is None:
            crud.create_user(db=db, username="tzadmin",
                             hashed_password=hash_password("tzpass12345"), global_role="admin")
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": "tzadmin", "password": "tzpass12345"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def settings(monkeypatch):
    """Stand in for the `general_settings` block the server was started with."""
    from unillm.proxy import auth as proxy_auth

    block = {}
    monkeypatch.setattr(proxy_auth, "_general_settings", block)
    return block


# ---------------------------------------------------------------------------
# Saying which instant it was
# ---------------------------------------------------------------------------

def test_a_naive_timestamp_is_serialized_as_utc():
    assert timefmt.iso_utc(datetime(2026, 9, 2, 13, 19, 54)) == "2026-09-02T13:19:54Z"


def test_an_aware_timestamp_is_converted_rather_than_relabelled():
    """A value that already knows its offset must not be stamped as UTC in place."""
    eastern = datetime(2026, 9, 2, 9, 19, 54, tzinfo=timezone(timedelta(hours=-4)))
    assert timefmt.iso_utc(eastern) == "2026-09-02T13:19:54Z"


def test_missing_timestamps_stay_missing():
    assert timefmt.iso_utc(None) is None


def test_api_timestamps_carry_an_offset(client, auth):
    """
    The regression itself. Without an offset the browser reads the value in its own
    zone, which is how a request from hours ago came out as "just now".
    """
    created_at = client.get("/api/users/me", headers=auth).json()["created_at"]
    assert created_at.endswith("Z"), created_at
    # And it must be parseable as one unambiguous instant.
    assert datetime.fromisoformat(created_at).tzinfo is not None


def test_audit_timestamps_carry_an_offset(client, auth):
    rows = client.get("/api/logs/audit?limit=5", headers=auth).json()
    rows = rows if isinstance(rows, list) else rows.get("audit", rows.get("items", []))
    assert rows, "the login above should have been audited"
    assert all(r["created_at"].endswith("Z") for r in rows)


def test_model_summary_timestamps_carry_an_offset(client, auth):
    """
    This one is built by hand in crud rather than by a response model, so it needed
    fixing separately — and it is exactly the page the wrong times were noticed on.
    """
    db = SessionLocal()
    try:
        crud.create_request_log(db=db, model="tz-probe", model_type="vertex-ai", status_code=200)
        crud.create_request_log(db=db, model="tz-probe", model_type="vertex-ai",
                                status_code=500, error_message="boom")
    finally:
        db.close()
    models = client.get("/api/models", headers=auth).json()
    probe = next(m for m in models if m["name"] == "tz-probe")
    assert probe["last_success_at"].endswith("Z")
    assert probe["last_failure_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Choosing which clock to show it on
# ---------------------------------------------------------------------------

def test_toronto_is_the_default(settings):
    assert timefmt.display_timezone() == "America/Toronto"


def test_the_config_can_choose_another_zone(settings):
    settings["display_timezone"] = "Europe/London"
    assert timefmt.display_timezone() == "Europe/London"


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "EST5EDT_typo", "", "   "])
def test_an_unusable_zone_falls_back_instead_of_raising(settings, value):
    """
    A typo in the config should make times read oddly, not take the log pages down.
    Every timestamp in the console goes through this.
    """
    settings["display_timezone"] = value
    assert timefmt.display_timezone() == "America/Toronto"


def test_the_console_is_told_which_zone_to_use(client, auth, settings):
    settings["display_timezone"] = "Europe/London"
    assert client.get("/api/config", headers=auth).json()["display_timezone"] == "Europe/London"
