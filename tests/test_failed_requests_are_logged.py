"""
A request that fails leaves a log row behind.

The request log is the only place an operator can answer "what happened to that
call". It used to hold successes and nothing else: every endpoint recorded its
failures with `background_tasks.add_task(...)` and then raised an HTTPException,
and FastAPI only attaches background tasks to the response an endpoint *returns* —
raise instead and the queued write is dropped. So a key denied a model, a rejected
parameter, an over-budget logprobs request and an upstream outage all completed
their round trip without leaving a trace, and the dashboard showed a 100% success
rate however badly the proxy was doing.

Failures now write inline. These tests exercise each way a request can be refused,
on both completion endpoints, and check the row is really there.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base, RequestLog
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.types import ChatCompletionResponse, Choice, Message, Usage


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
def admin_auth(client):
    db = SessionLocal()
    try:
        crud.create_user(db=db, username="logadmin",
                         hashed_password=hash_password("adminpass"), global_role="admin")
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": "logadmin", "password": "adminpass"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def project_id(client, admin_auth):
    return client.post("/api/projects", json={"name": "logging"}, headers=admin_auth).json()["id"]


@pytest.fixture(scope="module")
def api_key(client, admin_auth, project_id):
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": "open-key", "allowed_models": ["all"]},
                       headers=admin_auth).json()["api_key"]


@pytest.fixture(scope="module")
def restricted_key(client, admin_auth, project_id):
    """A key allowed one model, used to ask for another."""
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": "restricted-key", "allowed_models": ["other-model"]},
                       headers=admin_auth).json()["api_key"]


def _response():
    return ChatCompletionResponse(
        id="x", model="m", usage=Usage(prompt_tokens=3, completion_tokens=2),
        choices=[Choice(index=0, message=Message(role="assistant", content="hi"),
                        finish_reason="stop")],
    )


@pytest.fixture
def backend(monkeypatch):
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset()
    handler.SUPPORTED_TEXT_PARAMS = frozenset()
    handler.chat_completion = AsyncMock(return_value=_response())
    handler.text_completion = AsyncMock(return_value=_response())

    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": "served", "unillm_params": {"model": "gemini-2.5-flash"}},
    ])
    monkeypatch.setattr(ps, "general_settings", {})
    return handler


@pytest.fixture(autouse=True)
def clean_logs():
    """Each test reads the whole table, so it has to start empty."""
    db = SessionLocal()
    try:
        db.query(RequestLog).delete()
        db.commit()
    finally:
        db.close()


def rows():
    db = SessionLocal()
    try:
        return db.query(RequestLog).order_by(RequestLog.id).all()
    finally:
        db.close()


def only_row():
    got = rows()
    assert len(got) == 1, f"expected exactly one log row, got {len(got)}"
    return got[0]


def chat(client, key, **body):
    return client.post("/v1/chat/completions",
                       json={"model": "served", "messages": [{"role": "user", "content": "hi"}], **body},
                       headers={"Authorization": f"Bearer {key}"})


def text(client, key, **body):
    return client.post("/v1/completions",
                       json={"model": "served", "prompt": "hi", **body},
                       headers={"Authorization": f"Bearer {key}"})


# ---------------------------------------------------------------------------
# The served path still works the way it did
# ---------------------------------------------------------------------------

def test_a_served_request_is_logged(client, api_key, backend):
    assert chat(client, api_key).status_code == 200
    row = only_row()
    assert row.status_code == 200
    assert (row.prompt_tokens, row.completion_tokens) == (3, 2)
    assert row.error_message is None


def test_a_served_request_records_the_backend_it_reached(client, api_key, backend):
    chat(client, api_key)
    assert only_row().backend_model == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Upstream failures
# ---------------------------------------------------------------------------

def _upstream_error(status_code=429):
    request = httpx.Request("POST", "https://example.invalid/v1")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("upstream said no", request=request, response=response)


def test_an_upstream_failure_is_logged_with_its_status(client, api_key, backend):
    backend.chat_completion = AsyncMock(side_effect=_upstream_error(429))
    assert chat(client, api_key).status_code == 429
    row = only_row()
    assert row.status_code == 429
    assert "upstream said no" in row.error_message


def test_an_upstream_failure_on_a_stream_is_logged(client, api_key, backend):
    """
    Priming raises before the wrapper that logs streams ever runs, so this case
    fell between the two mechanisms and was lost by both.
    """
    backend.chat_completion = AsyncMock(side_effect=_upstream_error(503))
    assert chat(client, api_key, stream=True).status_code == 502
    row = only_row()
    assert row.status_code == 503
    assert row.stream is True


def test_an_upstream_failure_on_text_completions_is_logged(client, api_key, backend):
    backend.text_completion = AsyncMock(side_effect=_upstream_error(429))
    assert text(client, api_key).status_code == 429
    assert only_row().status_code == 429


def test_the_caller_still_sees_a_sanitized_error(client, api_key, backend):
    """The row keeps the upstream detail; the response must not repeat it."""
    backend.chat_completion = AsyncMock(side_effect=_upstream_error(429))
    body = chat(client, api_key).json()
    assert "upstream said no" not in str(body)
    assert "upstream said no" in only_row().error_message


# ---------------------------------------------------------------------------
# Requests the proxy refuses itself
# ---------------------------------------------------------------------------

def test_a_rejected_parameter_is_logged(client, api_key, backend):
    """The stub model serves no logprobs, so asking for them is a 400 from us."""
    assert chat(client, api_key, logprobs=True).status_code == 400
    row = only_row()
    assert row.status_code == 400
    assert "logprobs" in row.error_message


def test_an_over_budget_logprobs_request_is_logged(client, api_key, monkeypatch):
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset({"logprobs", "top_logprobs"})
    handler.chat_completion = AsyncMock(return_value=_response())
    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": "served", "unillm_params": {
            "model": "gemini-2.5-flash", "supports_logprobs": True}},
    ])
    monkeypatch.setattr(ps, "general_settings", {})

    r = chat(client, api_key, logprobs=True, top_logprobs=20, max_tokens=200000)
    assert r.status_code == 413
    assert only_row().status_code == 413


def test_a_model_the_key_may_not_use_is_logged(client, restricted_key, backend):
    r = chat(client, restricted_key)
    assert r.status_code == 403
    row = only_row()
    assert row.status_code == 403
    assert row.model == "served"
    # It never resolved to a backend, so those fields stay empty rather than
    # claiming the request reached one.
    assert row.backend_model is None
    assert row.model_type is None


def test_an_unconfigured_model_is_logged(client, api_key, monkeypatch):
    import unillm.proxy.proxy_server as ps
    monkeypatch.setattr(ps, "vertex_handlers", {})
    monkeypatch.setattr(ps.proxy_config, "model_list", [])

    r = client.post("/v1/chat/completions",
                    json={"model": "not-configured", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 404
    row = only_row()
    assert row.status_code == 404
    assert row.model == "not-configured"


def test_a_refusal_on_text_completions_is_logged(client, restricted_key, backend):
    assert text(client, restricted_key).status_code == 403
    assert only_row().status_code == 403


# ---------------------------------------------------------------------------
# A failure row is worth as much as a successful one
# ---------------------------------------------------------------------------

def test_a_failure_keeps_the_attribution_a_success_would_have(client, api_key, backend, project_id):
    backend.chat_completion = AsyncMock(side_effect=_upstream_error(429))
    chat(client, api_key, labels={"team": "search"})
    row = only_row()
    assert row.project_id == project_id
    assert row.api_key_name == "open-key"
    assert row.api_key_prefix == api_key[:8]
    assert row.labels == {"team": "search"}
    assert row.request_id
    assert row.latency_ms is not None


def test_a_failure_is_visible_through_the_logs_api(client, admin_auth, api_key, backend):
    backend.chat_completion = AsyncMock(side_effect=_upstream_error(429))
    chat(client, api_key)
    body = client.get("/api/logs/requests?status_code=429", headers=admin_auth).json()
    assert body["total"] == 1
    assert body["items"][0]["status_code"] == 429


def test_failures_reach_the_stats_by_status_breakdown(client, admin_auth, api_key, backend):
    chat(client, api_key)                                        # served
    backend.chat_completion = AsyncMock(side_effect=_upstream_error(429))
    chat(client, api_key)                                        # refused upstream
    stats = client.get("/api/logs/stats", headers=admin_auth).json()
    assert stats["by_status"] == {"200": 1, "429": 1}
    assert stats["total_requests"] == 2
