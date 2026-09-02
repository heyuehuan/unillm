"""
The logprobs size check follows what is actually sent, not what was asked for.

With `drop_params` enabled, a model that does not serve logprobs has them stripped
before the request goes upstream — the caller gets an ordinary completion instead
of a 400. The pre-flight size check still read the *request*, so a big
`max_tokens` plus a `logprobs` flag the server had already decided to ignore was
refused with a 413 for logprobs the response was never going to contain.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.types import (
    ChatCompletionResponse,
    Choice,
    CompletionResponse,
    Message,
    TextChoice,
    Usage,
)


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
def api_key(client):
    db = SessionLocal()
    try:
        crud.create_user(db=db, username="dropadmin",
                         hashed_password=hash_password("adminpass"), global_role="admin")
    finally:
        db.close()
    auth = {"Authorization": "Bearer " + client.post(
        "/api/auth/login",
        json={"username": "dropadmin", "password": "adminpass"}).json()["access_token"]}
    project_id = client.post("/api/projects", json={"name": "drop-params"}, headers=auth).json()["id"]
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": "drop-key", "allowed_models": ["all"]},
                       headers=auth).json()["api_key"]


@pytest.fixture
def backend(monkeypatch):
    """A handler that can do logprobs, behind models that are and are not allowed to."""
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset({"logprobs", "top_logprobs"})
    handler.SUPPORTED_TEXT_PARAMS = frozenset({"logprobs"})
    handler.chat_completion = AsyncMock(return_value=ChatCompletionResponse(
        id="x", model="m", usage=Usage(prompt_tokens=1, completion_tokens=1),
        choices=[Choice(index=0, message=Message(role="assistant", content="hi"),
                        finish_reason="stop")],
    ))
    handler.text_completion = AsyncMock(return_value=CompletionResponse(
        id="x", model="m", usage=Usage(prompt_tokens=1, completion_tokens=1),
        choices=[TextChoice(index=0, text="hi", finish_reason="stop")],
    ))

    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": "lp-off", "unillm_params": {"model": "gemini-2.5-flash"}},
        {"model_name": "lp-on", "unillm_params": {
            "model": "gemini-2.5-flash", "supports_logprobs": True}},
    ])
    return handler


@pytest.fixture
def drop_params(monkeypatch):
    import unillm.proxy.proxy_server as ps
    monkeypatch.setattr(ps, "general_settings", {"drop_params": True})


@pytest.fixture
def strict_params(monkeypatch):
    import unillm.proxy.proxy_server as ps
    monkeypatch.setattr(ps, "general_settings", {})


# A request whose logprobs, if they were returned, would be far over the cap.
HUGE = dict(logprobs=True, top_logprobs=20, max_tokens=200000)


def _chat(client, api_key, model, **body):
    return client.post("/v1/chat/completions",
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}], **body},
                       headers={"Authorization": f"Bearer {api_key}"})


def test_a_dropped_logprobs_request_is_served(client, api_key, backend, drop_params):
    r = _chat(client, api_key, "lp-off", **HUGE)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["logprobs"] is None


def test_the_dropped_params_never_reach_the_backend(client, api_key, backend, drop_params):
    _chat(client, api_key, "lp-off", **HUGE)
    kwargs = backend.chat_completion.call_args.kwargs
    assert "logprobs" not in kwargs and "top_logprobs" not in kwargs


def test_the_legacy_endpoint_agrees(client, api_key, backend, drop_params):
    r = client.post("/v1/completions",
                    json={"model": "lp-off", "prompt": "hi", "logprobs": 5, "max_tokens": 200000},
                    headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200, r.text
    assert "logprobs" not in backend.text_completion.call_args.kwargs


def test_without_drop_params_it_is_still_a_400_not_a_413(client, api_key, backend, strict_params):
    """
    The size check must not pre-empt the clearer error either: the real problem is
    that this model does not do logprobs at all.
    """
    r = _chat(client, api_key, "lp-off", **HUGE)
    assert r.status_code == 400, r.text
    assert "logprobs" in r.json()["detail"]


def test_a_model_that_really_returns_logprobs_is_still_capped(client, api_key, backend, drop_params):
    """The cap has not been weakened — only aimed at requests that will produce output."""
    r = _chat(client, api_key, "lp-on", **HUGE)
    assert r.status_code == 413
    backend.chat_completion.assert_not_called()
