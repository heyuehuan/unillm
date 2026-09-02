"""
Covers the two ways a logprobs response is kept small.

- `logprobs_format: "compact"`, the flat {token: logprob} shape
- `logprobs_max_bytes`, the per-request size cap

Between them they touch four layers, all exercised here: the conversion and budget
helpers, the ChoiceLogprobs serializer that hides the unused shape, the settings
resolution chain (database over config file over built-in default) with its admin
endpoints, and both completion endpoints end to end, streaming and not.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base
from unillm.llm.logprobs import (
    COMPACT_FORMAT,
    OPENAI_FORMAT,
    LogprobsBudget,
    compact_chat_payload,
    compact_choice_logprobs,
    compact_stream_chunk,
    min_bytes_estimate,
)
from unillm.proxy import server_settings
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.types import (
    ChatCompletionRequest,
    ChatCompletionTokenLogprob,
    ChoiceLogprobs,
    CompletionRequest,
    TopLogprob,
)


def _entry(token, logprob, alts=()):
    return ChatCompletionTokenLogprob(
        token=token, logprob=logprob,
        top_logprobs=[TopLogprob(token=t, logprob=lp) for t, lp in alts],
    )


@pytest.fixture(autouse=True)
def clean_settings():
    """
    Settings are cached in a module global for a few seconds, so a value written by
    one test would otherwise leak into the next one that reads it.
    """
    server_settings.set_config_settings({})
    yield
    server_settings.set_config_settings({})


# ---------------------------------------------------------------------------
# Compact conversion
# ---------------------------------------------------------------------------

def test_compact_collapses_alternatives_into_one_map():
    logprobs = ChoiceLogprobs(content=[
        _entry("Yes", -0.05, [("Yes", -0.05), ("No", -3.2)]),
    ])
    out = compact_choice_logprobs(logprobs)
    assert out.format == COMPACT_FORMAT
    assert out.tokens == ["Yes"]
    assert out.token_logprobs == [-0.05]
    assert out.top_logprobs == [{"Yes": -0.05, "No": -3.2}]


def test_compact_drops_the_openai_shape():
    """Both shapes present at once would double the payload it exists to shrink."""
    out = compact_choice_logprobs(ChoiceLogprobs(content=[_entry("a", -0.1)]))
    assert out.content is None


def test_compact_keeps_the_higher_logprob_on_a_decoded_collision():
    """Two token ids can decode to the same string; the map has room for one."""
    logprobs = ChoiceLogprobs(content=[
        _entry("yes", -0.5, [("yes", -0.5), ("yes", -4.0), ("no", -2.0)]),
    ])
    out = compact_choice_logprobs(logprobs)
    assert out.top_logprobs == [{"yes": -0.5, "no": -2.0}]


def test_compact_preserves_position_order():
    logprobs = ChoiceLogprobs(content=[_entry("a", -0.1), _entry("b", -0.2), _entry("c", -0.3)])
    assert compact_choice_logprobs(logprobs).tokens == ["a", "b", "c"]


def test_compact_of_nothing_is_nothing():
    assert compact_choice_logprobs(None) is None
    assert compact_choice_logprobs(ChoiceLogprobs()).content is None


def test_compact_payload_matches_the_parsed_conversion():
    payload = {"content": [
        {"token": "Yes", "logprob": -0.05,
         "top_logprobs": [{"token": "Yes", "logprob": -0.05}, {"token": "No", "logprob": -3.2}]},
    ]}
    assert compact_chat_payload(payload) == {
        "format": "compact", "tokens": ["Yes"], "token_logprobs": [-0.05],
        "top_logprobs": [{"Yes": -0.05, "No": -3.2}],
    }


def test_compact_stream_chunk_converts_chat_choices():
    chunk = {"choices": [{"index": 0, "logprobs": {
        "content": [{"token": "a", "logprob": -0.1,
                     "top_logprobs": [{"token": "a", "logprob": -0.1}]}]}}]}
    out = compact_stream_chunk(chunk)
    assert out["choices"][0]["logprobs"]["tokens"] == ["a"]


def test_compact_stream_chunk_leaves_legacy_choices_alone():
    """The legacy shape is already {token: logprob}; there is nothing to compact."""
    legacy = {"tokens": ["a"], "token_logprobs": [-0.1], "top_logprobs": [{"a": -0.1}]}
    chunk = {"choices": [{"index": 0, "logprobs": dict(legacy)}]}
    assert compact_stream_chunk(chunk)["choices"][0]["logprobs"] == legacy


def test_compact_is_smaller_on_the_wire():
    """The whole point: same information, materially fewer bytes."""
    content = [_entry(f"tok{i}", -0.1, [(f"alt{j}", -float(j)) for j in range(20)])
               for i in range(50)]
    verbose = len(json.dumps(ChoiceLogprobs(content=content).model_dump()))
    compact = len(json.dumps(compact_choice_logprobs(ChoiceLogprobs(content=content)).model_dump()))
    assert compact < verbose * 0.6


# ---------------------------------------------------------------------------
# Serialization: only the shape in use is visible
# ---------------------------------------------------------------------------

def test_openai_shape_does_not_leak_compact_keys():
    dumped = ChoiceLogprobs(content=[_entry("a", -0.1)]).model_dump()
    assert set(dumped) == {"content"}


def test_compact_shape_does_not_leak_a_null_content():
    dumped = compact_choice_logprobs(ChoiceLogprobs(content=[_entry("a", -0.1)])).model_dump()
    assert "content" not in dumped


def test_truncation_marker_is_only_present_when_it_happened():
    assert "truncated" not in ChoiceLogprobs(content=[_entry("a", -0.1)]).model_dump()
    assert ChoiceLogprobs(content=[], truncated=True, truncated_at=3).model_dump()["truncated_at"] == 3


# ---------------------------------------------------------------------------
# Pre-flight size estimate
# ---------------------------------------------------------------------------

def test_estimate_scales_with_tokens_and_width():
    small = min_bytes_estimate(100, 5, OPENAI_FORMAT)
    assert min_bytes_estimate(200, 5, OPENAI_FORMAT) == 2 * small
    assert min_bytes_estimate(100, 20, OPENAI_FORMAT) > small


def test_compact_estimates_smaller_than_openai():
    assert min_bytes_estimate(100, 20, COMPACT_FORMAT) < min_bytes_estimate(100, 20, OPENAI_FORMAT)


def test_no_estimate_without_a_token_ceiling():
    """With max_tokens unset there is no bound to estimate against."""
    assert min_bytes_estimate(None, 20, OPENAI_FORMAT) is None
    assert min_bytes_estimate(0, 20, OPENAI_FORMAT) is None


def test_the_estimate_is_a_floor_not_a_guess():
    """It must never exceed a real response, or it would reject requests that fit."""
    content = [_entry(f"token{i}", -0.12345, [(f"alt{j}", -float(j)) for j in range(5)])
               for i in range(30)]
    actual = len(json.dumps(ChoiceLogprobs(content=content).model_dump(exclude_none=True),
                            separators=(",", ":")).encode())
    assert min_bytes_estimate(30, 5, OPENAI_FORMAT) <= actual


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def _long_chat_logprobs(n=40):
    return ChoiceLogprobs(content=[_entry(f"tok{i}", -0.1, [("a", -0.1), ("b", -2.0)])
                                   for i in range(n)])


def test_no_cap_means_no_truncation():
    logprobs = _long_chat_logprobs()
    assert len(LogprobsBudget(None).apply_to_choice(logprobs).content) == 40


def test_a_generous_cap_leaves_the_response_whole():
    out = LogprobsBudget(1_000_000).apply_to_choice(_long_chat_logprobs())
    assert len(out.content) == 40
    assert out.truncated is None


def test_a_tight_cap_truncates_and_says_so():
    out = LogprobsBudget(400).apply_to_choice(_long_chat_logprobs())
    assert out.truncated is True
    assert 0 < len(out.content) < 40
    assert out.truncated_at == len(out.content)


def test_truncation_keeps_a_prefix_not_a_sample():
    out = LogprobsBudget(400).apply_to_choice(_long_chat_logprobs())
    assert [e.token for e in out.content] == [f"tok{i}" for i in range(len(out.content))]


@pytest.mark.parametrize("cap", [200, 400, 1000, 3000])
def test_the_result_fits_the_cap(cap):
    """The accounting has to cover the object and its commas, not just the positions."""
    out = LogprobsBudget(cap).apply_to_choice(_long_chat_logprobs())
    assert len(json.dumps(out.model_dump(), separators=(",", ":")).encode()) <= cap


@pytest.mark.parametrize("cap", [200, 400, 1000, 3000])
def test_the_compact_result_fits_the_cap(cap):
    out = LogprobsBudget(cap).apply_to_choice(compact_choice_logprobs(_long_chat_logprobs()))
    assert len(json.dumps(out.model_dump(), separators=(",", ":")).encode()) <= cap


def test_the_cap_covers_the_request_not_each_choice():
    """n>1 sends every choice down one connection, so they share one allowance."""
    budget = LogprobsBudget(400)
    first = budget.apply_to_choice(_long_chat_logprobs())
    second = budget.apply_to_choice(_long_chat_logprobs())
    assert len(first.content) > 0
    assert len(second.content) < len(first.content)


def test_the_compact_shape_fits_more_under_the_same_cap():
    cap = 400
    verbose = LogprobsBudget(cap).apply_to_choice(_long_chat_logprobs())
    compact = LogprobsBudget(cap).apply_to_choice(compact_choice_logprobs(_long_chat_logprobs()))
    assert len(compact.tokens) > len(verbose.content)


def test_legacy_payload_truncates_its_parallel_arrays_together():
    payload = {
        "tokens": [f"t{i}" for i in range(40)],
        "token_logprobs": [-0.1] * 40,
        "top_logprobs": [{"a": -0.1, "b": -2.0}] * 40,
        "text_offset": list(range(40)),
    }
    out = LogprobsBudget(200).apply_to_payload(payload)
    kept = len(out["tokens"])
    assert out["truncated"] is True
    assert kept < 40
    assert len(out["token_logprobs"]) == kept
    assert len(out["top_logprobs"]) == kept
    assert len(out["text_offset"]) == kept


def test_a_streaming_budget_is_spent_across_chunks():
    budget = LogprobsBudget(1000)
    kept = []
    for _ in range(10):
        chunk = {"choices": [{"index": 0, "logprobs": {"content": [
            {"token": "tok", "logprob": -0.1,
             "top_logprobs": [{"token": "a", "logprob": -0.1}, {"token": "b", "logprob": -2.0}]}
            for _ in range(3)]}}]}
        budget.apply_to_stream_chunk(chunk)
        kept.append(len(chunk["choices"][0]["logprobs"]["content"]))
    assert kept[0] == 3          # early chunks pass through whole
    assert kept[-1] == 0         # the allowance is gone by the end
    assert budget.truncated is True


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def test_chat_format_requires_logprobs():
    with pytest.raises(ValueError, match="logprobs_format"):
        ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                              logprobs_format="compact")


def test_chat_format_is_accepted_with_logprobs():
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                                logprobs=True, top_logprobs=5, logprobs_format="compact")
    assert req.logprobs_format == "compact"


def test_an_unknown_format_is_rejected():
    with pytest.raises(ValueError):
        ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}],
                              logprobs=True, logprobs_format="msgpack")


def test_the_legacy_endpoint_accepts_the_format_it_already_uses():
    assert CompletionRequest(model="m", prompt="hi", logprobs=3,
                             logprobs_format="compact").logprobs_format == "compact"


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        crud.delete_server_setting(session, server_settings.LOGPROBS_MAX_BYTES)
        server_settings.invalidate_cache()
        session.close()


KEY = server_settings.LOGPROBS_MAX_BYTES


def test_the_default_applies_when_nothing_is_set(db):
    assert server_settings.get_setting(db, KEY) == server_settings.DEFAULT_LOGPROBS_MAX_BYTES


def test_the_config_file_overrides_the_default(db):
    server_settings.set_config_settings({KEY: 4096})
    assert server_settings.get_setting(db, KEY) == 4096


def test_the_database_overrides_the_config_file(db):
    server_settings.set_config_settings({KEY: 4096})
    crud.set_server_setting(db, KEY, 8192)
    server_settings.invalidate_cache()
    assert server_settings.get_setting(db, KEY) == 8192


def test_clearing_the_override_falls_back_to_the_config_file(db):
    server_settings.set_config_settings({KEY: 4096})
    crud.set_server_setting(db, KEY, 8192)
    crud.delete_server_setting(db, KEY)
    server_settings.invalidate_cache()
    assert server_settings.get_setting(db, KEY) == 4096


def test_a_bad_config_value_does_not_break_requests(db):
    """A typo in the YAML should cost that setting its override, not the proxy."""
    server_settings.set_config_settings({KEY: "not-a-number"})
    assert server_settings.get_setting(db, KEY) == server_settings.DEFAULT_LOGPROBS_MAX_BYTES


def test_a_config_value_below_the_minimum_is_ignored(db):
    server_settings.set_config_settings({KEY: 10})
    assert server_settings.get_setting(db, KEY) == server_settings.DEFAULT_LOGPROBS_MAX_BYTES


def test_describe_reports_where_each_value_came_from(db):
    def source():
        return next(r for r in server_settings.describe(db) if r["key"] == KEY)["source"]

    assert source() == "default"
    server_settings.set_config_settings({KEY: 4096})
    assert source() == "config"
    crud.set_server_setting(db, KEY, 8192)
    assert source() == "database"


# ---------------------------------------------------------------------------
# Admin settings API
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_auth(client):
    db = SessionLocal()
    try:
        crud.create_user(db=db, username="limitsadmin",
                         hashed_password=hash_password("adminpass"), global_role="admin")
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": "limitsadmin", "password": "adminpass"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def user_auth(client):
    db = SessionLocal()
    try:
        crud.create_user(db=db, username="limitsuser",
                         hashed_password=hash_password("userpass"), global_role="user")
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": "limitsuser", "password": "userpass"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def api_key(client, admin_auth):
    project_id = client.post("/api/projects", json={"name": "limits"}, headers=admin_auth).json()["id"]
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": "limits-key", "allowed_models": ["all"]},
                       headers=admin_auth).json()["api_key"]


def test_an_admin_can_read_the_settings(client, admin_auth, db):
    rows = client.get("/api/settings", headers=admin_auth).json()
    assert any(r["key"] == KEY for r in rows)


def test_a_non_admin_cannot_read_the_settings(client, user_auth):
    assert client.get("/api/settings", headers=user_auth).status_code == 403


def test_a_non_admin_cannot_change_a_setting(client, user_auth):
    r = client.put(f"/api/settings/{KEY}", json={"value": 4096}, headers=user_auth)
    assert r.status_code == 403


def test_an_admin_change_takes_effect_immediately(client, admin_auth, db):
    r = client.put(f"/api/settings/{KEY}", json={"value": 4096}, headers=admin_auth)
    assert r.status_code == 200
    assert r.json()["value"] == 4096
    assert r.json()["source"] == "database"
    assert server_settings.get_setting(db, KEY) == 4096


def test_resetting_returns_the_setting_to_its_default(client, admin_auth, db):
    client.put(f"/api/settings/{KEY}", json={"value": 4096}, headers=admin_auth)
    r = client.delete(f"/api/settings/{KEY}", headers=admin_auth)
    assert r.status_code == 200
    assert r.json()["source"] == "default"
    assert r.json()["value"] == server_settings.DEFAULT_LOGPROBS_MAX_BYTES


def test_an_out_of_range_value_is_rejected(client, admin_auth):
    assert client.put(f"/api/settings/{KEY}", json={"value": 10}, headers=admin_auth).status_code == 422
    assert client.put(f"/api/settings/{KEY}", json={"value": 10 ** 12},
                      headers=admin_auth).status_code == 422


def test_a_non_numeric_value_is_rejected(client, admin_auth):
    assert client.put(f"/api/settings/{KEY}", json={"value": "big"},
                      headers=admin_auth).status_code == 422


def test_an_unknown_setting_is_a_404(client, admin_auth):
    assert client.put("/api/settings/nope", json={"value": 1}, headers=admin_auth).status_code == 404
    assert client.delete("/api/settings/nope", headers=admin_auth).status_code == 404


def test_changing_a_setting_is_audited(client, admin_auth, db):
    client.put(f"/api/settings/{KEY}", json={"value": 4096}, headers=admin_auth)
    actions = [a["action"] for a in client.get("/api/logs/audit", headers=admin_auth).json()["items"]]
    assert "setting_updated" in actions


def test_every_user_can_read_the_cap_from_the_config_endpoint(client, user_auth, db):
    body = client.get("/api/config", headers=user_auth).json()
    assert body["logprobs_max_bytes"] == server_settings.DEFAULT_LOGPROBS_MAX_BYTES


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def _chat_response(n_positions=1):
    from unillm.types import ChatCompletionResponse, Choice, Message, Usage
    return ChatCompletionResponse(
        id="x", model="m", usage=Usage(prompt_tokens=1, completion_tokens=1),
        choices=[Choice(index=0, message=Message(role="assistant", content="hi"),
                        finish_reason="stop",
                        logprobs=ChoiceLogprobs(content=[
                            _entry(f"tok{i}", -0.05, [("Yes", -0.05), ("No", -3.2)])
                            for i in range(n_positions)]))],
    )


@pytest.fixture
def stub_backend(monkeypatch):
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset({"logprobs", "top_logprobs"})
    handler.SUPPORTED_TEXT_PARAMS = frozenset({"logprobs"})
    handler.chat_completion = AsyncMock(return_value=_chat_response())
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


def test_the_default_response_shape_is_unchanged(client, api_key, stub_backend, db):
    r = _post(client, api_key, logprobs=True, top_logprobs=2)
    assert r.status_code == 200
    logprobs = r.json()["choices"][0]["logprobs"]
    assert "content" in logprobs and "tokens" not in logprobs


def test_asking_for_compact_returns_decoded_token_maps(client, api_key, stub_backend, db):
    r = _post(client, api_key, logprobs=True, top_logprobs=2, logprobs_format="compact")
    assert r.status_code == 200
    logprobs = r.json()["choices"][0]["logprobs"]
    assert logprobs["format"] == "compact"
    assert logprobs["top_logprobs"] == [{"Yes": -0.05, "No": -3.2}]
    assert "content" not in logprobs


def test_the_format_is_not_forwarded_to_the_backend(client, api_key, stub_backend, db):
    _post(client, api_key, logprobs=True, top_logprobs=2, logprobs_format="compact")
    assert "logprobs_format" not in stub_backend.chat_completion.call_args.kwargs


def test_an_impossible_request_is_refused_before_inference(client, api_key, stub_backend, db):
    r = _post(client, api_key, logprobs=True, top_logprobs=20, max_tokens=200000)
    assert r.status_code == 413
    assert "logprobs" in r.json()["detail"]
    stub_backend.chat_completion.assert_not_called()


def test_the_refusal_points_at_the_compact_format(client, api_key, stub_backend, db):
    r = _post(client, api_key, logprobs=True, top_logprobs=20, max_tokens=200000)
    assert "compact" in r.json()["detail"]


def test_a_request_within_the_cap_is_served(client, api_key, stub_backend, db):
    r = _post(client, api_key, logprobs=True, top_logprobs=2, max_tokens=100)
    assert r.status_code == 200


def test_compact_makes_a_refused_request_fit(client, api_key, stub_backend, db):
    """Same shape of ask, under the same cap, allowed only because it is smaller."""
    over = dict(logprobs=True, top_logprobs=20, max_tokens=3000)
    assert _post(client, api_key, **over).status_code == 413
    assert _post(client, api_key, logprobs_format="compact", **over).status_code == 200


def test_a_response_over_the_cap_is_truncated_and_marked(client, api_key, monkeypatch, db):
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset({"logprobs", "top_logprobs"})
    handler.chat_completion = AsyncMock(return_value=_chat_response(n_positions=200))
    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": "lp-on", "unillm_params": {
            "model": "gemini-2.5-flash", "supports_logprobs": True}},
    ])
    monkeypatch.setattr(ps, "general_settings", {})
    server_settings.set_config_settings({KEY: 2048})

    r = _post(client, api_key, logprobs=True, top_logprobs=2)
    assert r.status_code == 200
    logprobs = r.json()["choices"][0]["logprobs"]
    assert logprobs["truncated"] is True
    assert 0 < logprobs["truncated_at"] < 200
    assert len(logprobs["content"]) == logprobs["truncated_at"]


def _sse(chunks):
    async def _gen():
        for c in chunks:
            yield f"data: {json.dumps(c)}\n\n"
        yield "data: [DONE]\n\n"
    return _gen()


def _stream_logprobs(client, api_key, stub_backend, chunks, **body):
    stub_backend.chat_completion = AsyncMock(return_value=_sse(chunks))
    r = _post(client, api_key, stream=True, logprobs=True, top_logprobs=2, **body)
    assert r.status_code == 200
    out = []
    for line in r.text.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


def _chunk():
    return {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}, "logprobs": {
        "content": [{"token": "Yes", "logprob": -0.05, "top_logprobs": [
            {"token": "Yes", "logprob": -0.05}, {"token": "No", "logprob": -3.2}]}]}}]}


def test_streamed_chunks_can_be_compact(client, api_key, stub_backend, db):
    parsed = _stream_logprobs(client, api_key, stub_backend, [_chunk()], logprobs_format="compact")
    logprobs = parsed[0]["choices"][0]["logprobs"]
    assert logprobs["format"] == "compact"
    assert logprobs["top_logprobs"] == [{"Yes": -0.05, "No": -3.2}]


def test_streamed_chunks_keep_the_openai_shape_by_default(client, api_key, stub_backend, db):
    parsed = _stream_logprobs(client, api_key, stub_backend, [_chunk()])
    assert "content" in parsed[0]["choices"][0]["logprobs"]


def test_a_stream_stops_emitting_logprobs_once_the_cap_is_spent(client, api_key, stub_backend, db):
    server_settings.set_config_settings({KEY: 1024})
    parsed = _stream_logprobs(client, api_key, stub_backend, [_chunk() for _ in range(40)])
    kept = [len(c["choices"][0]["logprobs"]["content"]) for c in parsed]
    assert kept[0] == 1
    assert kept[-1] == 0
    assert any(c["choices"][0]["logprobs"].get("truncated") for c in parsed)


def test_a_stream_under_the_cap_is_untouched(client, api_key, stub_backend, db):
    parsed = _stream_logprobs(client, api_key, stub_backend, [_chunk() for _ in range(5)])
    assert all(len(c["choices"][0]["logprobs"]["content"]) == 1 for c in parsed)
    assert not any("truncated" in c["choices"][0]["logprobs"] for c in parsed)
