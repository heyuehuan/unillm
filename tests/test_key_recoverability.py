"""
Per-key reveal opt-in.

Every API key used to be stored Fernet-encrypted so that a project admin could
read it back later. That made the database hold a recoverable copy of every key
in the system, and with no dedicated UNILLM_ENCRYPTION_KEY the Fernet key is
derived from the JWT secret — so one host compromise recovered all of them.

Recoverability is now a per-key decision made at creation and defaulting to off,
under a deployment-wide veto (UNILLM_RECOVERABLE_KEYS=false) that turns the whole
feature off. These tests pin all three layers: the default, the opt-in, and the veto.
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


# --- the default is show-once ----------------------------------------------------

def test_keys_are_not_recoverable_by_default(client, admin_token, project_id, db):
    r = _create_key(client, admin_token, project_id, "default-key")
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("sk-")     # plaintext is returned once, here
    assert body["key"]["recoverable"] is False

    # Nothing decryptable was written. This is the property that matters: an
    # attacker with the database and the secret still cannot produce this key.
    row = db.query(APIKey).filter(APIKey.id == body["key"]["id"]).first()
    db.refresh(row)
    assert row.key_ciphertext is None


def test_revealing_a_show_once_key_returns_404(client, admin_token, project_id):
    key_id = _create_key(client, admin_token, project_id, "no-reveal").json()["key"]["id"]
    r = client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token))
    assert r.status_code == 404
    assert "revoke" in r.json()["detail"].lower()   # tells the admin what to do instead


def test_seeded_personal_key_is_not_recoverable(client, db):
    """A user's auto-created personal key is printed once, so it must not be stored."""
    user, plaintext = crud.create_user(db=db, username="rec-personal",
                                       hashed_password=hash_password("personal1"))
    assert plaintext.startswith("sk-")
    row = db.query(APIKey).filter(APIKey.project_id == user.personal_project_id).first()
    assert row.key_ciphertext is None


# --- opting a single key in ------------------------------------------------------

def test_a_key_can_opt_in_and_then_be_revealed(client, admin_token, project_id):
    r = _create_key(client, admin_token, project_id, "reveal-me", recoverable=True)
    body = r.json()
    assert body["key"]["recoverable"] is True

    revealed = client.get(f"/api/keys/{body['key']['id']}/reveal", headers=_auth(admin_token))
    assert revealed.status_code == 200
    # The revealed value must be the actual key, not merely well-formed.
    assert revealed.json()["api_key"] == body["api_key"]


def test_opting_one_key_in_does_not_affect_others(client, admin_token, project_id):
    opted = _create_key(client, admin_token, project_id, "opted", recoverable=True).json()["key"]
    plain = _create_key(client, admin_token, project_id, "plain").json()["key"]
    assert client.get(f"/api/keys/{opted['id']}/reveal", headers=_auth(admin_token)).status_code == 200
    assert client.get(f"/api/keys/{plain['id']}/reveal", headers=_auth(admin_token)).status_code == 404


def test_key_listing_reports_recoverability(client, admin_token, project_id):
    """The console needs this to show Reveal only where it will work."""
    keys = client.get(f"/api/projects/{project_id}/keys", headers=_auth(admin_token)).json()
    by_name = {k["name"]: k for k in keys}
    assert by_name["reveal-me"]["recoverable"] is True
    assert by_name["default-key"]["recoverable"] is False


def test_the_choice_is_audited(client, admin_token, project_id, db):
    _create_key(client, admin_token, project_id, "audited-recoverable", recoverable=True)
    _create_key(client, admin_token, project_id, "audited-show-once")
    rows, _ = crud.query_audit_logs(db, action="api_key_created")
    by_name = {r.detail["name"]: r.detail for r in rows if r.detail}
    assert by_name["audited-recoverable"]["recoverable"] is True
    assert by_name["audited-show-once"]["recoverable"] is False


# --- the deployment-wide veto ----------------------------------------------------

def test_veto_refuses_to_create_a_recoverable_key(client, admin_token, project_id, monkeypatch):
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    r = _create_key(client, admin_token, project_id, "vetoed", recoverable=True)
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"].lower()


def test_veto_still_allows_ordinary_show_once_keys(client, admin_token, project_id, monkeypatch):
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    r = _create_key(client, admin_token, project_id, "still-fine")
    assert r.status_code == 201
    assert r.json()["api_key"].startswith("sk-")


def test_veto_hides_reveal_for_keys_encrypted_before_it_was_set(client, admin_token, project_id, monkeypatch):
    """
    Turning the switch off must take effect immediately, including for keys that
    already carry ciphertext — otherwise disabling it would not actually stop
    anyone reading existing keys back.
    """
    key_id = _create_key(client, admin_token, project_id, "pre-existing", recoverable=True).json()["key"]["id"]
    assert client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token)).status_code == 200

    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    assert client.get(f"/api/keys/{key_id}/reveal", headers=_auth(admin_token)).status_code == 404
    keys = client.get(f"/api/projects/{project_id}/keys", headers=_auth(admin_token)).json()
    assert all(k["recoverable"] is False for k in keys)


# --- what the console asks for ---------------------------------------------------

def test_config_endpoint_reports_the_veto(client, admin_token, monkeypatch):
    assert client.get("/api/config", headers=_auth(admin_token)).json()["recoverable_keys_allowed"] is True
    monkeypatch.setenv("UNILLM_RECOVERABLE_KEYS", "false")
    assert client.get("/api/config", headers=_auth(admin_token)).json()["recoverable_keys_allowed"] is False


def test_config_endpoint_requires_authentication(client):
    assert client.get("/api/config").status_code == 401
