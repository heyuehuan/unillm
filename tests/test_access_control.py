"""
Cross-tenant authorization matrix.

The per-endpoint permission checks are individually covered elsewhere; this file
pins down the property that matters as a whole: a user with no membership in a
project can reach nothing belonging to it, and a member cannot exceed their
project role. It also covers token forgery and request-log scoping, neither of
which had a test.
"""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from unillm.config import get_jwt_secret
from unillm.db import crud
from unillm.db.database import init_db, engine, SessionLocal
from unillm.db.models import Base, RequestLog
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


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


@pytest.fixture(scope="module")
def env(client, db):
    """One project with an admin/developer/viewer, plus an unrelated outsider."""
    crud.create_user(db=db, username="ac-root", hashed_password=hash_password("rootpass123"),
                     global_role="admin")
    root = client.post("/api/auth/login",
                       json={"username": "ac-root", "password": "rootpass123"}).json()["access_token"]

    def mkuser(name):
        r = client.post("/api/users", headers=_auth(root),
                        json={"username": name, "password": "userpass123"})
        assert r.status_code == 201, r.text
        uid = r.json()["user"]["id"]
        tok = client.post("/api/auth/login",
                          json={"username": name, "password": "userpass123"}).json()["access_token"]
        return uid, tok

    lead_id, lead = mkuser("ac-lead")
    dev_id, dev = mkuser("ac-dev")
    obs_id, obs = mkuser("ac-obs")
    out_id, out = mkuser("ac-out")

    project = client.post("/api/projects", headers=_auth(root),
                          json={"name": "ac-project"}).json()
    pid = project["id"]
    for uid, role in ((lead_id, "admin"), (dev_id, "developer"), (obs_id, "viewer")):
        assert client.post(f"/api/projects/{pid}/members", headers=_auth(root),
                           json={"user_id": uid, "role": role}).status_code == 201

    key = client.post(f"/api/projects/{pid}/keys", headers=_auth(root),
                      json={"name": "ac-key"}).json()
    return {
        "root": root, "lead": lead, "dev": dev, "obs": obs, "out": out,
        "project_id": pid, "key_id": key["key"]["id"], "out_id": out_id, "dev_id": dev_id,
    }


# ── a non-member reaches nothing ───────────────────────────

@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/projects/{p}", None),
    ("get", "/api/projects/{p}/keys", None),
    ("get", "/api/projects/{p}/members", None),
    ("get", "/api/projects/{p}/member-candidates", None),
    ("get", "/api/keys/{k}/reveal", None),
    ("delete", "/api/keys/{k}", None),
    ("put", "/api/keys/{k}", {"name": "taken-over"}),
    ("put", "/api/projects/{p}", {"name": "taken-over"}),
    ("post", "/api/projects/{p}/keys", {"name": "mine-now"}),
])
def test_non_member_is_denied(client, env, method, path, body):
    url = path.format(p=env["project_id"], k=env["key_id"])
    r = getattr(client, method)(url, headers=_auth(env["out"]), **({"json": body} if body else {}))
    assert r.status_code == 403, f"{method.upper()} {url} -> {r.status_code}"


def test_non_member_cannot_add_themselves(client, env):
    r = client.post(f"/api/projects/{env['project_id']}/members", headers=_auth(env["out"]),
                    json={"user_id": env["out_id"], "role": "admin"})
    assert r.status_code == 403


# ── members cannot exceed their project role ───────────────

def test_viewer_can_read_but_not_manage(client, env):
    pid, kid = env["project_id"], env["key_id"]
    assert client.get(f"/api/projects/{pid}", headers=_auth(env["obs"])).status_code == 200
    assert client.get(f"/api/keys/{kid}/reveal", headers=_auth(env["obs"])).status_code == 403
    assert client.post(f"/api/projects/{pid}/keys", headers=_auth(env["obs"]),
                       json={"name": "x"}).status_code == 403


def test_developer_cannot_reveal_or_manage_keys(client, env):
    pid, kid = env["project_id"], env["key_id"]
    assert client.get(f"/api/projects/{pid}", headers=_auth(env["dev"])).status_code == 200
    assert client.get(f"/api/keys/{kid}/reveal", headers=_auth(env["dev"])).status_code == 403
    assert client.delete(f"/api/keys/{kid}", headers=_auth(env["dev"])).status_code == 403
    assert client.post(f"/api/projects/{pid}/members", headers=_auth(env["dev"]),
                       json={"user_id": env["out_id"], "role": "admin"}).status_code == 403


def test_project_admin_is_not_a_global_admin(client, env):
    assert client.get("/api/users", headers=_auth(env["lead"])).status_code == 403
    assert client.post("/api/projects", headers=_auth(env["lead"]),
                       json={"name": "sneaky"}).status_code == 403
    assert client.put(f"/api/users/{env['dev_id']}", headers=_auth(env["lead"]),
                      json={"global_role": "admin"}).status_code == 403


def test_self_update_cannot_grant_global_role(client, env):
    r = client.put("/api/users/me", headers=_auth(env["dev"]),
                   json={"name": "Dev", "global_role": "admin"})
    assert r.status_code == 200
    assert r.json()["user"]["global_role"] == "user"


# ── token forgery ──────────────────────────────────────────

def test_forged_and_malformed_tokens_are_rejected(client):
    claims = {"sub": "1", "tv": 0, "exp": int(time.time()) + 3600}
    wrong_sig = jwt.encode(claims, "not-the-real-secret", algorithm="HS256")
    expired = jwt.encode({**claims, "exp": int(time.time()) - 60}, get_jwt_secret(), algorithm="HS256")
    for token in ("", "not-a-jwt", wrong_sig, expired):
        assert client.get("/api/users/me", headers=_auth(token)).status_code == 401
    assert client.get("/api/users/me").status_code == 401


def test_unsigned_alg_none_token_is_rejected(client):
    import base64, json as _json
    b64 = lambda d: base64.urlsafe_b64encode(_json.dumps(d).encode()).rstrip(b"=").decode()
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'tv': 0})}."
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 401


def test_disabling_an_account_kills_its_existing_token(client, env, db):
    r = client.post("/api/users", headers=_auth(env["root"]),
                    json={"username": "ac-doomed", "password": "userpass123"})
    uid = r.json()["user"]["id"]
    token = client.post("/api/auth/login",
                        json={"username": "ac-doomed", "password": "userpass123"}).json()["access_token"]
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 200
    assert client.put(f"/api/users/{uid}", headers=_auth(env["root"]),
                      json={"active": False}).status_code == 200
    assert client.get("/api/users/me", headers=_auth(token)).status_code == 401


# ── request logs stay inside the caller's projects ─────────

def test_request_logs_are_scoped_to_accessible_projects(client, env, db):
    other = client.post("/api/projects", headers=_auth(env["root"]),
                        json={"name": "ac-other"}).json()
    db.add(RequestLog(request_id="ac-mine", project_id=env["project_id"], model="gpt-4o"))
    db.add(RequestLog(request_id="ac-theirs", project_id=other["id"], model="gpt-4o"))
    db.commit()

    seen = client.get("/api/logs/requests", headers=_auth(env["dev"])).json()["items"]
    ids = {i["request_id"] for i in seen}
    assert "ac-mine" in ids and "ac-theirs" not in ids

    # Asking for a project the caller cannot see returns nothing, not everything.
    scoped = client.get(f"/api/logs/requests?project_ids={other['id']}",
                        headers=_auth(env["dev"])).json()
    assert scoped["items"] == []

    admin_view = client.get("/api/logs/requests", headers=_auth(env["root"])).json()["items"]
    assert {"ac-mine", "ac-theirs"} <= {i["request_id"] for i in admin_view}
