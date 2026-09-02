"""
Key recovery is a deployment-wide setting.

Whether an API key can be read back later is decided once, by
UNILLM_RECOVERABLE_KEYS, and applies to every key alike — keys created through the
console, and the personal key each account is given at creation. It defaults to on.

It used to be a per-key opt-in defaulting to off, which meant two keys sitting in
the same list behaved differently at the moment somebody needed one back, and the
auto-created personal key could never be recovered at all. These tests pin the
current rule: the flag decides, nothing else, and turning it off stops reveal for
keys that already carry ciphertext as well as for new ones.
"""
import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app
from unillm.db.database import init_db, engine, SessionLocal
from unillm.db.models import APIKey, Base
from unillm.db import crud
from unillm.proxy.api_routes import hash_password


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


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin_token(client, db):
    crud.create_user(db=db, username="rec-admin", hashed_password=hash_password("adminpass1"),
                     global_role="admin")
    r = client.post("/api/auth/login", json={"username": "rec-admin", "password": "adminpass1"})
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def project_id(client, admin_token):
    return client.post("/api/projects", json={"name": "rec-project"}, headers=_auth(admin_token)).json()["id"]


def _create_key(client, token, project_id, name, **extra):
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": name, **extra}, headers=_auth(token))


# --- recovery is on by default ---------------------------------------------------

def test_a_new_key_is_recoverable(client, admin_token, project_id, db):
    r = _create_key(client, admin_token, project_id, "ordinary-key")
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("sk-")
    assert body["key"]["recoverable"] is True

    row = db.query(APIKey).filter(APIKey.id == body["key"]["id"]).first()
    db.refresh(row)
    assert row.key_ciphertext is not None


def test_revealing_returns_the_actual_key(client, admin_token, project_id):
    created = _create_key(client, admin_token, project_id, "reveal-me").json()
    r = client.get(f"/api/keys/{created['key']['id']}/reveal", headers=_auth(admin_token))
    assert r.status_code == 200
    # The value has to be the key itself, not merely something well-formed.
    assert r.json()["api_key"] == created["api_key"]


def test_the_seeded_personal_key_is_recoverable_too(client, db):
    """
    The key handed out with a new account is the one most likely to be mislaid,
    since nobody chose to create it. It follows the same rule as every other key.
    """
    user, plaintext = crud.create_user(db=db, username="rec-personal",
                                       hashed_password=hash_password("personal1"))
    assert plaintext.startswith("sk-")
    row = db.query(APIKey).filter(APIKey.project_id == user.personal_project_id).first()
    assert row.key_ciphertext is not None
    assert crud.decrypt_api_key(row.key_ciphertext) == plaintext


def test_key_listing_reports_recoverability(client, admin_token, project_id):
    """The console needs this to show Reveal only where it will work."""
    keys = client.get(f"/api/projects/{project_id}/keys", headers=_auth(admin_token)).json()
    assert keys and all(k["recoverable"] is True for k in keys)


def test_creation_is_audited_with_the_outcome(client, admin_token, project_id, db):
    _create_key(client, admin_token, project_id, "audited-key")
    rows, _ = crud.query_audit_logs(db, action="api_key_created")
    by_name = {r.detail["name"]: r.detail for r in rows if r.detail}
    assert by_name["audited-key"]["recoverable"] is True


# --- turning the deployment switch off --------------------------------------------

def test_switching_recovery_off_stops_new_keys_storing_anything(client, admin_token, project_id, db, monkeypatch):
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    r = _create_key(client, admin_token, project_id, "show-once")
    assert r.status_code == 201
    assert r.json()["api_key"].startswith("sk-")
    assert r.json()["key"]["recoverable"] is False

    row = db.query(APIKey).filter(APIKey.id == r.json()["key"]["id"]).first()
    db.refresh(row)
    assert row.key_ciphertext is None


def test_a_key_created_while_recovery_was_off_cannot_be_revealed_later(client, admin_token, project_id, monkeypatch):
    """Turning the switch back on cannot reach backwards: nothing was stored."""
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    key_id = _create_key(client, admin_token, project_id, "nothing-stored").json()["key"]["id"]
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "true")
    r = client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token))
    assert r.status_code == 404
    assert "revoke" in r.json()["detail"].lower()   # tells the admin what to do instead


def test_switching_recovery_off_hides_keys_encrypted_before_it_was_set(client, admin_token, project_id, monkeypatch):
    """
    Turning the switch off must take effect immediately, including for keys that
    already carry ciphertext — otherwise disabling it would not actually stop
    anyone reading existing keys back.
    """
    key_id = _create_key(client, admin_token, project_id, "pre-existing").json()["key"]["id"]
    assert client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token)).status_code == 200

    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    assert client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token)).status_code == 404
    keys = client.get(f"/api/projects/{project_id}/keys", headers=_auth(admin_token)).json()
    assert all(k["recoverable"] is False for k in keys)


# --- what the console asks for ---------------------------------------------------

def test_config_endpoint_reports_the_setting(client, admin_token, monkeypatch):
    assert client.get("/api/config", headers=_auth(admin_token)).json()["recoverable_keys_allowed"] is True
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    assert client.get("/api/config", headers=_auth(admin_token)).json()["recoverable_keys_allowed"] is False


def test_config_endpoint_requires_authentication(client):
    assert client.get("/api/config").status_code == 401
