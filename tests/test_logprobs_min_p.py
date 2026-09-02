"""
Covers `logprobs_min_p`, the response-side probability floor that thins the
alternatives `top_logprobs` returns.

- The floor conversion (probability -> log-probability), including the 0 edge case
- Filtering the parsed chat shape, the legacy flat shape, and streaming chunks
- The chosen token's own logprob surviving regardless of the floor
- Request validation: a floor with no alternatives to filter is rejected
- End to end through /v1/chat/completions and /v1/completions, streaming and not
"""
import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.llm.logprobs import (
    filter_chat_logprobs_payload,
    filter_choice_logprobs,
    filter_legacy_logprobs,
    filter_stream_chunk,
    min_logprob_for,
)
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.types import (
    ChatCompletionRequest,
    ChatCompletionTokenLogprob,
    ChoiceLogprobs,
    CompletionRequest,
    TopLogprob,
)


# ---------------------------------------------------------------------------
# Threshold conversion
# ---------------------------------------------------------------------------

def test_probability_floor_becomes_a_log_floor():
    assert min_logprob_for(0.0001) == pytest.approx(math.log(0.0001))
    assert min_logprob_for(0.5) == pytest.approx(math.log(0.5))


def test_no_floor_means_no_filtering():
    assert min_logprob_for(None) is None


def test_zero_floor_is_not_a_filter():
    """log(0) is -inf and would raise; 0 admits everything, so it means 'off'."""
    assert min_logprob_for(0.0) is None


def test_floor_of_one_keeps_only_certainties():
    assert min_logprob_for(1.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Filtering the parsed chat shape
# ---------------------------------------------------------------------------

def _entry(token, logprob, alts):
    return ChatCompletionTokenLogprob(
        token=token,
        logprob=logprob,
        top_logprobs=[TopLogprob(token=t, logprob=lp) for t, lp in alts],
    )


def _chat_logprobs():
    # p = 0.61, 0.22, 0.0067, 4.5e-5 for the alternatives below
    return ChoiceLogprobs(content=[
        _entry("a", -0.5, [("a", -0.5), ("b", -1.5), ("c", -5.0), ("d", -10.0)]),
    ])


def test_alternatives_below_the_floor_are_dropped():
    out = filter_choice_logprobs(_chat_logprobs(), min_logprob_for(0.01))
    kept = [alt.token for alt in out.content[0].top_logprobs]
    assert kept == ["a", "b"]


def test_a_low_floor_keeps_everything():
    out = filter_choice_logprobs(_chat_logprobs(), min_logprob_for(1e-6))
    assert len(out.content[0].top_logprobs) == 4


def test_the_chosen_token_keeps_its_logprob_below_the_floor():
    """A sampled token can sit deep in the tail; dropping it would lose the one
    number every caller needs."""
    lp = ChoiceLogprobs(content=[_entry("rare", -12.0, [("common", -0.1)])])
    out = filter_choice_logprobs(lp, min_logprob_for(0.5))
    assert out.content[0].token == "rare"
    assert out.content[0].logprob == -12.0


def test_filtering_can_empty_the_alternatives_without_losing_the_position():
    lp = ChoiceLogprobs(content=[_entry("x", -0.1, [("y", -20.0)])])
    out = filter_choice_logprobs(lp, min_logprob_for(0.5))
    assert len(out.content) == 1
    assert out.content[0].top_logprobs == []


def test_no_floor_leaves_the_object_untouched():
    lp = _chat_logprobs()
    assert filter_choice_logprobs(lp, None) is lp
    assert len(lp.content[0].top_logprobs) == 4


def test_none_logprobs_survive_filtering():
    assert filter_choice_logprobs(None, min_logprob_for(0.5)) is None


# ---------------------------------------------------------------------------
# Filtering raw payloads (streaming) and the legacy shape
# ---------------------------------------------------------------------------

def test_chat_payload_dict_is_filtered():
    payload = {"content": [{"token": "a", "logprob": -0.5, "top_logprobs": [
        {"token": "a", "logprob": -0.5}, {"token": "z", "logprob": -20.0}]}]}
    out = filter_chat_logprobs_payload(payload, min_logprob_for(0.01))
    assert [a["token"] for a in out["content"][0]["top_logprobs"]] == ["a"]


def test_legacy_shape_filters_alternatives_but_not_chosen_tokens():
    payload = {
        "tokens": ["a", "b"],
        "token_logprobs": [-0.5, -14.0],
        "top_logprobs": [{"a": -0.5, "z": -20.0}, {"b": -14.0, "y": -0.2}],
        "text_offset": [0, 1],
    }
    out = filter_legacy_logprobs(payload, min_logprob_for(0.01))
    assert out["top_logprobs"] == [{"a": -0.5}, {"y": -0.2}]
    # the parallel arrays that carry the sampled tokens are untouched
    assert out["token_logprobs"] == [-0.5, -14.0]
    assert out["tokens"] == ["a", "b"]


def test_legacy_entries_of_an_unknown_shape_pass_through():
    """A backend that doesn't follow the legacy format must not have data dropped."""
    payload = {"top_logprobs": [None, "unexpected"]}
    out = filter_legacy_logprobs(payload, min_logprob_for(0.5))
    assert out["top_logprobs"] == [None, "unexpected"]


def test_stream_chunk_detects_the_chat_shape():
    chunk = {"choices": [{"logprobs": {"content": [
        {"token": "a", "logprob": -0.1, "top_logprobs": [
            {"token": "a", "logprob": -0.1}, {"token": "z", "logprob": -20.0}]}]}}]}
    out = filter_stream_chunk(chunk, min_logprob_for(0.01))
    assert len(out["choices"][0]["logprobs"]["content"][0]["top_logprobs"]) == 1


def test_stream_chunk_detects_the_legacy_shape():
    chunk = {"choices": [{"logprobs": {"tokens": ["a"], "token_logprobs": [-0.1],
                                       "top_logprobs": [{"a": -0.1, "z": -20.0}]}}]}
    out = filter_stream_chunk(chunk, min_logprob_for(0.01))
    assert out["choices"][0]["logprobs"]["top_logprobs"] == [{"a": -0.1}]


def test_stream_chunk_without_logprobs_is_untouched():
    chunk = {"choices": [{"delta": {"content": "hi"}}]}
    assert filter_stream_chunk(chunk, min_logprob_for(0.5)) == chunk


def test_usage_only_chunk_survives():
    """The final chunk of an OpenAI stream has no choices at all."""
    chunk = {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    assert filter_stream_chunk(chunk, min_logprob_for(0.5)) == chunk


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def test_chat_floor_requires_top_logprobs():
    with pytest.raises(ValueError, match="logprobs_min_p"):
        ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                              logprobs=True, logprobs_min_p=0.01)


def test_chat_floor_rejects_a_zero_top_logprobs():
    """top_logprobs=0 returns no alternatives, so there is nothing to filter."""
    with pytest.raises(ValueError, match="logprobs_min_p"):
        ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                              logprobs=True, top_logprobs=0, logprobs_min_p=0.01)


def test_chat_floor_is_accepted_alongside_top_logprobs():
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                                logprobs=True, top_logprobs=5, logprobs_min_p=0.01)
    assert req.logprobs_min_p == 0.01


def test_chat_floor_must_be_a_probability():
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                                  logprobs=True, top_logprobs=5, logprobs_min_p=bad)


def test_text_floor_requires_logprobs():
    with pytest.raises(ValueError, match="logprobs_min_p"):
        CompletionRequest(model="m", prompt="hi", logprobs_min_p=0.01)


def test_text_floor_is_accepted_alongside_logprobs():
    assert CompletionRequest(model="m", prompt="hi", logprobs=3,
                             logprobs_min_p=0.01).logprobs_min_p == 0.01


# ---------------------------------------------------------------------------
# End to end
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
        crud.create_user(db=db, username="minpadmin",
                         hashed_password=hash_password("adminpass"), global_role="admin")
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": "minpadmin", "password": "adminpass"}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    project_id = client.post("/api/projects", json={"name": "minp"}, headers=auth).json()["id"]
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": "minp-key", "allowed_models": ["all"]},
                       headers=auth).json()["api_key"]


def _chat_response_with_logprobs():
    """A stub chat response whose single choice carries four alternatives."""
    from unillm.types import ChatCompletionResponse, Choice, Message, Usage
    return ChatCompletionResponse(
        id="x", model="m", usage=Usage(prompt_tokens=1, completion_tokens=1),
        choices=[Choice(index=0, message=Message(role="assistant", content="hi"),
                        finish_reason="stop", logprobs=_chat_logprobs())],
    )


@pytest.fixture
def stub_backend(monkeypatch):
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset({"logprobs", "top_logprobs"})
    handler.SUPPORTED_TEXT_PARAMS = frozenset({"logprobs"})
    handler.chat_completion = AsyncMock(return_value=_chat_response_with_logprobs())
    handler.text_completion = AsyncMock()

    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": "lp-on", "unillm_params": {
            "model": "gemini-2.5-flash", "supports_logprobs": True}},
    ])
    monkeypatch.setattr(ps, "general_settings", {})
    return handler


def _post(client, api_key, **body):
    return client.post(
        "/v1/chat/completions",
        json={"model": "lp-on", "messages": [{"role": "user", "content": "hi"}], **body},
        headers={"Authorization": f"Bearer {api_key}"},
    )


def test_chat_response_is_thinned_by_the_floor(client, api_key, stub_backend):
    r = _post(client, api_key, logprobs=True, top_logprobs=4, logprobs_min_p=0.01)
    assert r.status_code == 200
    alts = r.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    assert [a["token"] for a in alts] == ["a", "b"]


def test_chat_response_is_untouched_without_a_floor(client, api_key, stub_backend):
    r = _post(client, api_key, logprobs=True, top_logprobs=4)
    assert r.status_code == 200
    assert len(r.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]) == 4


def test_the_floor_is_not_forwarded_to_the_backend(client, api_key, stub_backend):
    """It is a response filter — sending it upstream would be an unknown param."""
    _post(client, api_key, logprobs=True, top_logprobs=4, logprobs_min_p=0.01)
    assert "logprobs_min_p" not in stub_backend.chat_completion.call_args.kwargs


def test_a_floor_of_zero_reaches_the_client_unfiltered(client, api_key, stub_backend):
    r = _post(client, api_key, logprobs=True, top_logprobs=4, logprobs_min_p=0)
    assert r.status_code == 200
    assert len(r.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]) == 4


def test_chat_floor_without_top_logprobs_is_a_422(client, api_key, stub_backend):
    r = _post(client, api_key, logprobs=True, logprobs_min_p=0.01)
    assert r.status_code == 422
    stub_backend.chat_completion.assert_not_called()


def _sse(chunks):
    async def _gen():
        for c in chunks:
            yield f"data: {json.dumps(c)}\n\n"
        yield "data: [DONE]\n\n"
    return _gen()


def test_streamed_chunks_are_thinned(client, api_key, stub_backend):
    chunk = {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"},
             "logprobs": {"content": [{"token": "a", "logprob": -0.5, "top_logprobs": [
                 {"token": "a", "logprob": -0.5}, {"token": "z", "logprob": -20.0}]}]}}]}
    stub_backend.chat_completion = AsyncMock(return_value=_sse([chunk]))

    r = _post(client, api_key, logprobs=True, top_logprobs=4,
              logprobs_min_p=0.01, stream=True)
    assert r.status_code == 200
    payloads = [json.loads(line[6:]) for line in r.text.splitlines()
                if line.startswith("data: ") and not line.endswith("[DONE]")]
    alts = payloads[0]["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    assert [a["token"] for a in alts] == ["a"]


def test_streamed_chunks_are_untouched_without_a_floor(client, api_key, stub_backend):
    chunk = {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"},
             "logprobs": {"content": [{"token": "a", "logprob": -0.5, "top_logprobs": [
                 {"token": "a", "logprob": -0.5}, {"token": "z", "logprob": -20.0}]}]}}]}
    stub_backend.chat_completion = AsyncMock(return_value=_sse([chunk]))

    r = _post(client, api_key, logprobs=True, top_logprobs=4, stream=True)
    payloads = [json.loads(line[6:]) for line in r.text.splitlines()
                if line.startswith("data: ") and not line.endswith("[DONE]")]
    assert len(payloads[0]["choices"][0]["logprobs"]["content"][0]["top_logprobs"]) == 2


def test_text_completion_response_is_thinned(client, api_key, stub_backend):
    from unillm.types import CompletionResponse, TextChoice, Usage
    stub_backend.text_completion = AsyncMock(return_value=CompletionResponse(
        id="x", model="m", usage=Usage(prompt_tokens=1, completion_tokens=1),
        choices=[TextChoice(index=0, text="hi", finish_reason="stop", logprobs={
            "tokens": ["a"], "token_logprobs": [-0.5],
            "top_logprobs": [{"a": -0.5, "z": -20.0}], "text_offset": [0]})],
    ))

    r = client.post("/v1/completions",
                    json={"model": "lp-on", "prompt": "hi", "logprobs": 4,
                          "logprobs_min_p": 0.01},
                    headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["logprobs"]["top_logprobs"] == [{"a": -0.5}]
