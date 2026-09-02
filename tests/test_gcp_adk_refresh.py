"""
Recovering expired Google Cloud credentials from the console.

The credentials behind every Gemini call are a user login Google expires roughly
daily. Until now the only cure was shell access to the proxy host; these endpoints
move it into the console, which raises three questions worth testing:

  * Does it notice? A credential failure has to be told apart from an ordinary one,
    and a deployment that is merely idle must not look broken.
  * Is it gated? Spawning gcloud on the proxy host is offered when something is
    actually wrong, to users and admins, and never to viewers.
  * Does it work? The gcloud sign-in is an interactive process driven over a pipe,
    including the case where it finishes without anyone pasting a code.

The last one runs against a stand-in `gcloud` that reproduces the real command's
conversation — a URL, a prompt, an exit code — so the driver is exercised end to
end without a browser or a real Google login.
"""

import asyncio
import os
import stat
import textwrap
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy import gcp_adk
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


def _auth(client, username, role):
    db = SessionLocal()
    try:
        if crud.get_user_by_username(db, username) is None:
            crud.create_user(db=db, username=username,
                             hashed_password=hash_password("adkpass123"), global_role=role)
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": username, "password": "adkpass123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin_auth(client):
    return _auth(client, "adkadmin", "admin")


@pytest.fixture(scope="module")
def user_auth(client):
    return _auth(client, "adkuser", "user")


@pytest.fixture(scope="module")
def viewer_auth(client):
    return _auth(client, "adkviewer", "viewer")


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no logged traffic, no health memory, and no session."""
    db = SessionLocal()
    try:
        db.query(gcp_adk.RequestLog).delete()
        db.commit()
    finally:
        db.close()
    gcp_adk.reset_health_result()
    gcp_adk._active_session = None
    yield
    gcp_adk._active_session = None


def run_session_test(body):
    """
    Run an async test body, ending the gcloud session inside the same event loop.

    A subprocess belongs to the loop that spawned it. Cleaning up from a later
    asyncio.run() reaches across loops and raises instead of stopping the process,
    so the teardown has to happen here rather than in the fixture.
    """
    async def main():
        try:
            return await body()
        finally:
            await gcp_adk.clear_session()

    return asyncio.run(main())


@pytest.fixture
def enabled(monkeypatch):
    """Turn the feature on for the running proxy, as the YAML config would."""
    from unillm.proxy import proxy_server

    monkeypatch.setitem(proxy_server.general_settings, "gcp_adk", {
        "enabled": True,
        "service_account": "runner@example.iam.gserviceaccount.com",
        "health_model": "gemini-2.5-flash-lite",
        "stale_after_seconds": 3600,
    })
    return gcp_adk.load_config()


def _log(status_code=200, error_message=None, minutes_ago=5, model_type="vertex-ai"):
    db = SessionLocal()
    try:
        row = crud.create_request_log(
            db=db, model="gemini-2.5-flash-lite", model_type=model_type,
            status_code=status_code, error_message=error_message,
        )
        row.created_at = gcp_adk._utcnow() - timedelta(minutes=minutes_ago)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Telling a credential failure from any other kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status_code, message", [
    (500, "RefreshError: ('invalid_grant: Token has been expired or revoked.', {})"),
    (500, "DefaultCredentialsError: Could not automatically determine credentials"),
    (401, "Upstream model provider rejected the request"),
    (403, None),
])
def test_credential_failures_are_recognized(status_code, message):
    assert gcp_adk.is_auth_failure(status_code, message) is True


@pytest.mark.parametrize("status_code, message", [
    (400, "'top_logprobs' is only allowed when 'logprobs' is true"),
    (500, "ReadTimeout: the model took too long"),
    (413, "Request too large"),
    (None, None),
])
def test_ordinary_failures_are_not_credential_failures(status_code, message):
    """
    The narrow half of the test, and the one that matters.

    If a timeout or a rejected parameter counted, every busy deployment would show
    a standing "re-authenticate" prompt and the signal would mean nothing.
    """
    assert gcp_adk.is_auth_failure(status_code, message) is False


# ---------------------------------------------------------------------------
# Signal (a): what the request log says
# ---------------------------------------------------------------------------

def test_expired_credentials_are_detected_from_the_log(enabled):
    _log(status_code=500, error_message="RefreshError: invalid_grant", minutes_ago=10)
    db = SessionLocal()
    try:
        state = gcp_adk.status(db, enabled)
    finally:
        db.close()
    assert state["needs_refresh"] is True
    assert state["reasons"]


def test_a_recent_success_means_the_credentials_still_work(enabled):
    """A refresh that worked at all in the window settles the question."""
    _log(status_code=500, error_message="RefreshError: invalid_grant", minutes_ago=30)
    _log(status_code=200, minutes_ago=2)
    db = SessionLocal()
    try:
        state = gcp_adk.status(db, enabled)
    finally:
        db.close()
    assert state["needs_refresh"] is False


def test_an_old_failure_falls_out_of_the_window(enabled):
    _log(status_code=500, error_message="RefreshError: invalid_grant", minutes_ago=180)
    db = SessionLocal()
    try:
        state = gcp_adk.status(db, enabled)
    finally:
        db.close()
    assert state["needs_refresh"] is False


def test_non_vertex_traffic_is_ignored(enabled):
    """A vLLM failure says nothing about Google credentials, and vice versa."""
    _log(status_code=500, error_message="RefreshError: invalid_grant",
         minutes_ago=5, model_type="vllm")
    db = SessionLocal()
    try:
        state = gcp_adk.status(db, enabled)
    finally:
        db.close()
    assert state["needs_refresh"] is False


# ---------------------------------------------------------------------------
# Signal (b): the health test
# ---------------------------------------------------------------------------

def test_a_failed_health_test_unlocks_the_refresh(enabled):
    """
    The health test is the escape hatch when the log is silent.

    An idle deployment logs nothing, so signal (a) cannot fire — pressing the button
    is what turns "we don't know" into "the credentials are dead".
    """
    gcp_adk._last_health = gcp_adk.HealthResult(
        healthy=False, checked_at=gcp_adk._utcnow(), model="gemini-2.5-flash-lite",
        latency_ms=42, error="RefreshError: invalid_grant", auth_related=True,
    )
    db = SessionLocal()
    try:
        state = gcp_adk.status(db, enabled)
    finally:
        db.close()
    assert state["needs_refresh"] is True


def test_a_health_test_that_failed_for_another_reason_does_not(enabled):
    gcp_adk._last_health = gcp_adk.HealthResult(
        healthy=False, checked_at=gcp_adk._utcnow(), model="gemini-2.5-flash-lite",
        latency_ms=42, error="ReadTimeout", auth_related=False,
    )
    db = SessionLocal()
    try:
        state = gcp_adk.status(db, enabled)
    finally:
        db.close()
    assert state["needs_refresh"] is False


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def test_status_is_readable_by_anyone_signed_in(client, viewer_auth, enabled):
    response = client.get("/api/gcp-adk-refresh/status", headers=viewer_auth)
    assert response.status_code == 200
    assert response.json()["health_prompt"] == "Health test, reply 'hi' only."


def test_viewers_cannot_start_a_sign_in(client, viewer_auth, enabled):
    _log(status_code=500, error_message="RefreshError: invalid_grant")
    assert client.post("/api/gcp-adk-refresh/start", headers=viewer_auth).status_code == 403
    assert client.post("/api/gcp-adk-refresh/health-test", headers=viewer_auth).status_code == 403


def test_everything_is_404_when_the_feature_is_off(client, admin_auth):
    """Disabled means the endpoints are not there, not that they answer 403."""
    assert client.post("/api/gcp-adk-refresh/start", headers=admin_auth).status_code == 404
    assert client.post("/api/gcp-adk-refresh/health-test", headers=admin_auth).status_code == 404
    assert client.get("/api/gcp-adk-refresh/status", headers=admin_auth).json()["enabled"] is False


def test_a_healthy_deployment_will_not_start_a_sign_in(client, user_auth, enabled):
    """
    The gate that keeps this a recovery tool.

    Without it, "run gcloud on the proxy host" would be a button anyone could press
    at any time, on a deployment with nothing wrong with it.
    """
    _log(status_code=200, minutes_ago=1)
    response = client.post("/api/gcp-adk-refresh/start", headers=user_auth)
    assert response.status_code == 409
    assert "healthy" in response.json()["detail"]


def test_only_admins_may_force_past_the_gate(client, user_auth, enabled):
    _log(status_code=200, minutes_ago=1)
    response = client.post("/api/gcp-adk-refresh/start?force=true", headers=user_auth)
    assert response.status_code == 403


def test_an_unknown_session_is_404(client, user_auth, enabled):
    assert client.get("/api/gcp-adk-refresh/session/nope", headers=user_auth).status_code == 404


# ---------------------------------------------------------------------------
# Driving gcloud
# ---------------------------------------------------------------------------

def _fake_gcloud(tmp_path, body):
    script = tmp_path / "gcloud"
    script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(script)


PROMPTING_GCLOUD = """
    import sys
    print("Go to the following link in your browser:")
    print("    https://accounts.google.com/o/oauth2/auth?client_id=fake&code_challenge=abc")
    sys.stdout.write("Once finished, enter the verification code provided in your browser: ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "4/0AgoodCode":
        print("\\nCredentials saved to file: [adc.json]")
        sys.exit(0)
    print("\\nERROR: invalid_grant")
    sys.exit(1)
"""

SELF_COMPLETING_GCLOUD = """
    import sys, time
    print("Your browser has been opened to visit:")
    print("    https://accounts.google.com/o/oauth2/auth?client_id=fake")
    sys.stdout.flush()
    time.sleep(0.2)
    print("Credentials saved to file: [adc.json]")
    sys.exit(0)
"""


def _config(gcloud_path, **overrides):
    return gcp_adk.AdkConfig(
        service_account="runner@example.iam.gserviceaccount.com",
        health_model="gemini-2.5-flash-lite",
        gcloud_path=gcloud_path,
        enabled=True,
        **overrides,
    )


def test_the_sign_in_url_is_captured_and_the_code_completes_it(tmp_path, monkeypatch):
    refreshed = []
    monkeypatch.setattr(gcp_adk, "_on_credentials_refreshed",
                        lambda: asyncio.sleep(0, result=refreshed.append(True)))
    config = _config(_fake_gcloud(tmp_path, PROMPTING_GCLOUD))

    async def run():
        session = await gcp_adk.start_session(config, started_by="tester")
        for _ in range(100):
            if session.state == gcp_adk.STATE_AWAITING_CODE:
                break
            await asyncio.sleep(0.05)
        assert session.state == gcp_adk.STATE_AWAITING_CODE
        assert session.url.startswith("https://accounts.google.com/o/oauth2/auth")
        await session.submit_code("4/0AgoodCode")
        for _ in range(100):
            if session.finished:
                break
            await asyncio.sleep(0.05)
        return session

    session = run_session_test(run)
    assert session.state == gcp_adk.STATE_SUCCEEDED
    # The whole point of the feature: the proxy uses the new credentials without
    # being restarted, which only happens if the cached ones were dropped.
    assert refreshed == [True]


def test_a_sign_in_that_completes_on_its_own_is_detected(tmp_path, monkeypatch):
    """
    gcloud can finish without ever asking for a code — the browser flow completed
    by itself. Nobody has anything to paste, so the session has to reach 'succeeded'
    from the process exiting alone.
    """
    monkeypatch.setattr(gcp_adk, "_on_credentials_refreshed",
                        lambda: asyncio.sleep(0))
    config = _config(_fake_gcloud(tmp_path, SELF_COMPLETING_GCLOUD))

    async def run():
        session = await gcp_adk.start_session(config)
        for _ in range(100):
            if session.finished:
                break
            await asyncio.sleep(0.05)
        return session

    session = run_session_test(run)
    assert session.state == gcp_adk.STATE_SUCCEEDED
    assert session.url


def test_a_rejected_code_fails_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(gcp_adk, "_on_credentials_refreshed", lambda: asyncio.sleep(0))
    config = _config(_fake_gcloud(tmp_path, PROMPTING_GCLOUD))

    async def run():
        session = await gcp_adk.start_session(config)
        for _ in range(100):
            if session.state == gcp_adk.STATE_AWAITING_CODE:
                break
            await asyncio.sleep(0.05)
        await session.submit_code("wrong")
        for _ in range(100):
            if session.finished:
                break
            await asyncio.sleep(0.05)
        return session

    session = run_session_test(run)
    assert session.state == gcp_adk.STATE_FAILED
    assert "invalid_grant" in session.output


def test_a_missing_gcloud_fails_the_session_rather_than_the_server(tmp_path):
    config = _config(str(tmp_path / "definitely-not-here"))
    session = run_session_test(lambda: gcp_adk.start_session(config))
    assert session.state == gcp_adk.STATE_FAILED
    assert "not installed" in session.error


def test_two_operators_join_the_same_sign_in(tmp_path, monkeypatch):
    """
    Two people reacting to the same outage must not fight over the ADC file — the
    second start returns the first session rather than spawning a second gcloud.
    """
    monkeypatch.setattr(gcp_adk, "_on_credentials_refreshed", lambda: asyncio.sleep(0))
    config = _config(_fake_gcloud(tmp_path, PROMPTING_GCLOUD))

    async def run():
        first = await gcp_adk.start_session(config, started_by="alice")
        second = await gcp_adk.start_session(config, started_by="bob")
        return first, second

    first, second = run_session_test(run)
    assert first is second
    assert second.started_by == "alice"


@pytest.mark.parametrize("prompt", [
    "Enter authorization code: ",
    "Once finished, enter the verification code provided in your browser: ",
    "Enter the authorization code: ",
])
def test_every_gcloud_wording_of_the_code_prompt_is_recognized(enabled, prompt):
    """
    gcloud has changed this prompt between versions, and it arrives without a newline
    so there is no line to match on. Recognizing only one wording left the console
    showing the URL with no box to paste the code into — a dead end, on the flow the
    whole feature exists for.
    """
    session = gcp_adk.RefreshSession(enabled)
    session.state = gcp_adk.STATE_AWAITING_URL
    session._absorb("Go to:\n    https://accounts.google.com/o/oauth2/auth?x=1\n\n" + prompt)
    assert session.state == gcp_adk.STATE_AWAITING_CODE


def test_ordinary_output_is_not_mistaken_for_the_prompt(enabled):
    session = gcp_adk.RefreshSession(enabled)
    session.state = gcp_adk.STATE_AWAITING_URL
    session._absorb("Go to the following link in your browser:\n"
                    "    https://accounts.google.com/o/oauth2/auth?x=1\n")
    assert session.state == gcp_adk.STATE_AWAITING_URL


def test_the_impersonated_service_account_reaches_the_command_line(enabled):
    session = gcp_adk.RefreshSession(enabled)
    argv = session.command()
    assert argv[:4] == ["gcloud", "auth", "application-default", "login"]
    assert "--no-launch-browser" in argv
    assert "--impersonate-service-account=runner@example.iam.gserviceaccount.com" in argv


def test_token_shaped_output_is_scrubbed():
    """
    gcloud is not supposed to echo tokens, but its output is streamed straight to a
    browser from a process handling credentials — so it is filtered, not trusted.
    """
    scrubbed = gcp_adk.scrub("token ya29.a0AfB_byC123456 and refresh 1//0gLongRefreshToken")
    assert "ya29." not in scrubbed
    assert "1//0g" not in scrubbed
    assert "[redacted]" in scrubbed
