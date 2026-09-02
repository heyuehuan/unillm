"""
Covers logprobs support across the request path:

- Param negotiation (unillm/llm/params.py): capability x config gating, drop_params
- Request validation: top_logprobs requires logprobs
- Per-backend translation both ways:
    vLLM        — native OpenAI shape, passthrough
    Vertex REST — logprobs/top_logprobs <-> responseLogprobs/logprobs, logprobsResult
    Vertex KMS  — the same via the SDK's snake_case config and proto attributes
- End-to-end through /v1/chat/completions: 400 when a model isn't configured for
  logprobs, forwarded when it is, dropped when drop_params is set
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.llm.params import (
    UnsupportedParamsError,
    enabled_optional_params,
    resolve_optional_params,
)
from unillm.llm.vertex_ai import VertexAIHandler
from unillm.llm.vertex_ai_kms import VertexAIKMSHandler
from unillm.llm.vllm import VLLMHandler
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.types import ChatCompletionRequest


# ---------------------------------------------------------------------------
# Param negotiation
# ---------------------------------------------------------------------------

def _resolve(requested, capable, enabled, drop_params=False):
    return resolve_optional_params(
        requested, capable=capable, enabled=enabled,
        model="m", provider="vllm", drop_params=drop_params,
    )


def test_param_passes_when_capable_and_enabled():
    out = _resolve({"logprobs": True}, {"logprobs"}, {"logprobs"})
    assert out == {"logprobs": True}


def test_capable_but_not_configured_is_rejected():
    """The backend could do it, but the model didn't opt in — error names that fix."""
    with pytest.raises(UnsupportedParamsError) as exc:
        _resolve({"logprobs": True}, {"logprobs"}, set())
    assert exc.value.params == ["logprobs"]
    assert "supports_logprobs" in exc.value.message


def test_not_capable_is_rejected_even_when_configured():
    """Config can't grant a capability the handler has no way to express."""
    with pytest.raises(UnsupportedParamsError) as exc:
        _resolve({"logprobs": 3}, set(), {"logprobs"})
    assert "does not support parameters" in exc.value.message
    assert "supports_logprobs" not in exc.value.message


def test_drop_params_strips_instead_of_raising():
    assert _resolve({"logprobs": True}, {"logprobs"}, set(), drop_params=True) == {}
    assert _resolve({"logprobs": True}, set(), {"logprobs"}, drop_params=True) == {}


def test_unrequested_params_never_fail():
    """A param the client never sent is not something to reject the request over."""
    assert _resolve({}, set(), set()) == {}


def test_mixed_request_reports_both_reasons():
    exc = pytest.raises(
        UnsupportedParamsError,
        _resolve, {"logprobs": True, "top_logprobs": 5}, {"logprobs"}, set(),
    ).value
    assert exc.params == ["logprobs", "top_logprobs"]
    assert "does not support parameters" in exc.message  # top_logprobs: not capable
    assert "is not configured for parameters" in exc.message  # logprobs: not enabled


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (None, False),
    ("true", True), ("True", True), ("yes", True), ("on", True),
    ("false", False), ("no", False), ("", False),
])
def test_supports_logprobs_config_parsing(value, expected):
    """YAML bools and the string forms a hand-edited config tends to grow."""
    params = {} if value is None else {"supports_logprobs": value}
    assert bool(enabled_optional_params(params)) is expected


def test_text_completions_enable_only_the_legacy_param():
    """Legacy completions have an int `logprobs` and no `top_logprobs` at all."""
    cfg = {"supports_logprobs": True}
    assert enabled_optional_params(cfg) == {"logprobs", "top_logprobs"}
    assert enabled_optional_params(cfg, text=True) == {"logprobs"}


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def test_top_logprobs_requires_logprobs():
    with pytest.raises(ValueError):
        ChatCompletionRequest(
            model="m", messages=[{"role": "user", "content": "hi"}], top_logprobs=3
        )


def test_top_logprobs_upper_bound_enforced():
    with pytest.raises(ValueError):
        ChatCompletionRequest(
            model="m", messages=[{"role": "user", "content": "hi"}],
            logprobs=True, top_logprobs=21,
        )


# ---------------------------------------------------------------------------
# vLLM: native OpenAI shape
# ---------------------------------------------------------------------------

def test_vllm_forwards_logprobs():
    body = VLLMHandler()._build_chat_request(
        model="m", messages=[], temperature=None, top_p=None, max_tokens=None,
        stop=None, stream=False, logprobs=True, top_logprobs=5,
    )
    assert body["logprobs"] is True
    assert body["top_logprobs"] == 5


def test_vllm_withholds_top_logprobs_when_logprobs_off():
    """vLLM 400s on top_logprobs without logprobs; don't hand it that request."""
    body = VLLMHandler()._build_chat_request(
        model="m", messages=[], temperature=None, top_p=None, max_tokens=None,
        stop=None, stream=False, logprobs=False, top_logprobs=5,
    )
    assert body["logprobs"] is False
    assert "top_logprobs" not in body


def test_vllm_parses_response_logprobs():
    parsed = VLLMHandler()._parse_chat_response({
        "id": "x", "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
            "logprobs": {"content": [{
                "token": "hi", "logprob": -0.25, "bytes": [104, 105],
                "top_logprobs": [{"token": "hey", "logprob": -1.5, "bytes": [104, 101, 121]}],
            }]},
        }],
    })
    lp = parsed.choices[0].logprobs
    assert lp.content[0].token == "hi"
    assert lp.content[0].logprob == -0.25
    assert lp.content[0].bytes == [104, 105]
    assert lp.content[0].top_logprobs[0].token == "hey"


def test_vllm_text_completion_passes_legacy_logprobs_through():
    """Legacy /v1/completions logprobs are a flat object; vLLM speaks it natively."""
    legacy = {
        "tokens": ["hi"], "token_logprobs": [-0.25],
        "top_logprobs": [{"hi": -0.25, "hey": -1.5}], "text_offset": [0],
    }
    http_response = MagicMock(status_code=200)
    http_response.json.return_value = {
        "id": "x", "model": "m",
        "choices": [{"index": 0, "text": "hi", "finish_reason": "stop", "logprobs": legacy}],
        "usage": {},
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=http_response)

    handler = VLLMHandler()
    handler._get_http_client = AsyncMock(return_value=http_client)
    resp = asyncio.run(handler.text_completion(model="m", prompt="hi", logprobs=3))

    assert http_client.post.call_args.kwargs["json"]["logprobs"] == 3
    assert resp.choices[0].logprobs == legacy


def test_vllm_response_without_logprobs_stays_none():
    parsed = VLLMHandler()._parse_chat_response({
        "id": "x", "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    })
    assert parsed.choices[0].logprobs is None


# ---------------------------------------------------------------------------
# Vertex REST: name collision on "logprobs" between the two APIs
# ---------------------------------------------------------------------------

def test_vertex_maps_to_gemini_generation_config():
    """OpenAI's bool logprobs -> responseLogprobs; int top_logprobs -> logprobs."""
    config = VertexAIHandler()._build_generation_config(logprobs=True, top_logprobs=5)
    assert config["responseLogprobs"] is True
    assert config["logprobs"] == 5


def test_vertex_omits_topk_when_logprobs_off():
    config = VertexAIHandler()._build_generation_config(logprobs=False, top_logprobs=5)
    assert config["responseLogprobs"] is False
    assert "logprobs" not in config


def test_vertex_omits_topk_when_top_logprobs_is_zero():
    """OpenAI's top_logprobs=0 means 'no alternatives'; Gemini's count starts at 1."""
    config = VertexAIHandler()._build_generation_config(logprobs=True, top_logprobs=0)
    assert config["responseLogprobs"] is True
    assert "logprobs" not in config


def test_vertex_converts_logprobs_result():
    """Gemini's two parallel lists zip by position into OpenAI's nested shape."""
    lp = VertexAIHandler()._convert_logprobs({
        "chosenCandidates": [
            {"token": "Hello", "logProbability": -0.1},
            {"token": " world", "logProbability": -0.4},
        ],
        "topCandidates": [
            {"candidates": [
                {"token": "Hello", "logProbability": -0.1},
                {"token": "Hi", "logProbability": -2.0},
            ]},
            {"candidates": [{"token": " world", "logProbability": -0.4}]},
        ],
    })
    assert [t.token for t in lp.content] == ["Hello", " world"]
    assert lp.content[0].logprob == -0.1
    assert [a.token for a in lp.content[0].top_logprobs] == ["Hello", "Hi"]
    assert len(lp.content[1].top_logprobs) == 1


def test_vertex_handles_chosen_without_top_candidates():
    """logprobs=true without a top-k count returns chosen tokens only."""
    lp = VertexAIHandler()._convert_logprobs({
        "chosenCandidates": [{"token": "a", "logProbability": -0.5}]
    })
    assert lp.content[0].token == "a"
    assert lp.content[0].top_logprobs == []


def test_vertex_tolerates_short_top_candidates():
    """topCandidates shorter than chosenCandidates must not IndexError."""
    lp = VertexAIHandler()._convert_logprobs({
        "chosenCandidates": [
            {"token": "a", "logProbability": -0.1},
            {"token": "b", "logProbability": -0.2},
        ],
        "topCandidates": [{"candidates": [{"token": "a", "logProbability": -0.1}]}],
    })
    assert len(lp.content) == 2
    assert lp.content[1].top_logprobs == []


@pytest.mark.parametrize("payload", [None, {}, {"avgLogprobs": -0.3}])
def test_vertex_returns_none_without_per_token_data(payload):
    """avgLogprobs is always present and is not per-token data — not mapped."""
    assert VertexAIHandler()._convert_logprobs(payload) is None


def test_vertex_response_carries_logprobs_onto_the_choice():
    resp = VertexAIHandler()._convert_gemini_response_to_openai({
        "candidates": [{
            "content": {"parts": [{"text": "Hello"}]},
            "finishReason": "STOP",
            "logprobsResult": {
                "chosenCandidates": [{"token": "Hello", "logProbability": -0.1}]
            },
        }],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    }, model="gemini-2.5-flash")
    assert resp.choices[0].logprobs.content[0].token == "Hello"


def test_vertex_stream_chunk_puts_logprobs_beside_delta():
    """OpenAI streaming carries logprobs at choice level, not inside delta."""
    chunk = VertexAIHandler()._convert_stream_chunk({
        "candidates": [{
            "content": {"parts": [{"text": "Hi"}]},
            "logprobsResult": {
                "chosenCandidates": [{"token": "Hi", "logProbability": -0.2}]
            },
        }]
    }, model="gemini-2.5-flash")
    choice = chunk["choices"][0]
    assert choice["delta"] == {"content": "Hi"}
    assert choice["logprobs"]["content"][0]["token"] == "Hi"
    assert "logprobs" not in choice["delta"]


def test_vertex_stream_chunk_omits_logprobs_when_absent():
    chunk = VertexAIHandler()._convert_stream_chunk(
        {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]}, model="m"
    )
    assert "logprobs" not in chunk["choices"][0]


# ---------------------------------------------------------------------------
# Vertex KMS: same mapping through the SDK
# ---------------------------------------------------------------------------

def test_kms_uses_snake_case_generation_config():
    config = VertexAIKMSHandler()._build_generation_config(logprobs=True, top_logprobs=4)
    assert config["response_logprobs"] is True
    assert config["logprobs"] == 4


def test_kms_omits_topk_when_top_logprobs_is_zero():
    config = VertexAIKMSHandler()._build_generation_config(logprobs=True, top_logprobs=0)
    assert config["response_logprobs"] is True
    assert "logprobs" not in config


def test_kms_converts_proto_logprobs():
    """The SDK exposes attributes, not dict keys, and uses log_probability."""
    logprobs_result = SimpleNamespace(
        chosen_candidates=[SimpleNamespace(token="Hello", log_probability=-0.1)],
        top_candidates=[SimpleNamespace(candidates=[
            SimpleNamespace(token="Hello", log_probability=-0.1),
            SimpleNamespace(token="Hi", log_probability=-2.0),
        ])],
    )
    lp = VertexAIKMSHandler()._convert_logprobs(logprobs_result)
    assert lp.content[0].token == "Hello"
    assert [a.token for a in lp.content[0].top_logprobs] == ["Hello", "Hi"]


def test_kms_treats_unset_proto_as_no_logprobs():
    """Unset protos read as empty rather than None; both mean 'not requested'."""
    assert VertexAIKMSHandler()._convert_logprobs(None) is None
    assert VertexAIKMSHandler()._convert_logprobs(
        SimpleNamespace(chosen_candidates=[], top_candidates=[])
    ) is None


# ---------------------------------------------------------------------------
# End-to-end through the proxy
# ---------------------------------------------------------------------------

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
        crud.create_user(db=db, username="lpadmin",
                         hashed_password=hash_password("adminpass"), global_role="admin")
    finally:
        db.close()
    r = client.post("/api/auth/login", json={"username": "lpadmin", "password": "adminpass"})
    token = r.json()["access_token"]
    r = client.post("/api/projects", json={"name": "lp"},
                    headers={"Authorization": f"Bearer {token}"})
    project_id = r.json()["id"]
    r = client.post(f"/api/projects/{project_id}/keys",
                    json={"name": "lp-key", "allowed_models": ["all"]},
                    headers={"Authorization": f"Bearer {token}"})
    return r.json()["api_key"]


@pytest.fixture
def stub_backend(monkeypatch):
    """
    Configure two models — one opted into logprobs, one not — and capture the kwargs
    the handler receives so we can assert on what actually reached the backend.
    """
    import unillm.proxy.proxy_server as ps

    response = MagicMock()
    response.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    response.model_dump = lambda: {"id": "x", "object": "chat.completion", "choices": []}

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset({"logprobs", "top_logprobs"})
    handler.SUPPORTED_TEXT_PARAMS = frozenset()
    handler.chat_completion = AsyncMock(return_value=response)
    handler.text_completion = AsyncMock(return_value=response)

    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": "lp-on", "unillm_params": {
            "model": "gemini-2.5-flash", "supports_logprobs": True}},
        {"model_name": "lp-off", "unillm_params": {"model": "gemini-3-flash"}},
    ])
    monkeypatch.setattr(ps, "general_settings", {})
    return handler


def _post(client, api_key, model, **body):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **body},
        headers={"Authorization": f"Bearer {api_key}"},
    )


def test_logprobs_forwarded_when_model_opts_in(client, api_key, stub_backend):
    r = _post(client, api_key, "lp-on", logprobs=True, top_logprobs=3)
    assert r.status_code == 200
    kwargs = stub_backend.chat_completion.call_args.kwargs
    assert kwargs["logprobs"] is True
    assert kwargs["top_logprobs"] == 3


def test_logprobs_rejected_when_model_does_not_opt_in(client, api_key, stub_backend):
    r = _post(client, api_key, "lp-off", logprobs=True)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "lp-off" in detail and "logprobs" in detail
    stub_backend.chat_completion.assert_not_called()


def test_rejection_does_not_leak_backend_model_or_config_key(client, api_key, stub_backend):
    """Operator detail (backend model, config flag) belongs in the log, not the response."""
    r = _post(client, api_key, "lp-off", logprobs=True)
    detail = r.json()["detail"]
    assert "supports_logprobs" not in detail
    assert "gemini-3-flash" not in detail


def test_request_without_logprobs_unaffected_on_unconfigured_model(client, api_key, stub_backend):
    """The gate must only fire on params the caller actually asked for."""
    r = _post(client, api_key, "lp-off")
    assert r.status_code == 200
    assert "logprobs" not in stub_backend.chat_completion.call_args.kwargs


def test_explicit_logprobs_false_is_not_a_request_for_logprobs(client, api_key, stub_backend):
    """`logprobs: false` asks for none — failing it against an opted-out model is wrong."""
    r = _post(client, api_key, "lp-off", logprobs=False)
    assert r.status_code == 200
    assert "logprobs" not in stub_backend.chat_completion.call_args.kwargs


def test_drop_params_completes_the_request_without_logprobs(client, api_key, stub_backend, monkeypatch):
    import unillm.proxy.proxy_server as ps
    monkeypatch.setattr(ps, "general_settings", {"drop_params": True})

    r = _post(client, api_key, "lp-off", logprobs=True)
    assert r.status_code == 200
    assert "logprobs" not in stub_backend.chat_completion.call_args.kwargs


def _post_text(client, api_key, model, **body):
    return client.post(
        "/v1/completions",
        json={"model": model, "prompt": "hi", **body},
        headers={"Authorization": f"Bearer {api_key}"},
    )


def test_text_logprobs_rejected_when_backend_cannot_serve_them(client, api_key, stub_backend):
    """The model opts in, but a Gemini-backed handler has no legacy shape to fill."""
    r = _post_text(client, api_key, "lp-on", logprobs=3)
    assert r.status_code == 400
    stub_backend.text_completion.assert_not_called()


def test_text_logprobs_zero_is_still_a_request(client, api_key, stub_backend):
    """Unlike the chat bool, `logprobs: 0` on legacy completions is an explicit ask."""
    r = _post_text(client, api_key, "lp-on", logprobs=0)
    assert r.status_code == 400


def test_text_request_without_logprobs_is_unaffected(client, api_key, stub_backend):
    r = _post_text(client, api_key, "lp-on")
    assert r.status_code == 200
    assert "logprobs" not in stub_backend.text_completion.call_args.kwargs


def test_text_logprobs_forwarded_when_backend_and_config_allow(client, api_key, stub_backend):
    stub_backend.SUPPORTED_TEXT_PARAMS = frozenset({"logprobs"})
    r = _post_text(client, api_key, "lp-on", logprobs=3)
    assert r.status_code == 200
    assert stub_backend.text_completion.call_args.kwargs["logprobs"] == 3
