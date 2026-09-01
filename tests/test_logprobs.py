"""
Logprobs across the backends, and the negotiation in front of them.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from unillm.llm.params import (
    UnsupportedParamsError,
    enabled_optional_params,
    resolve_optional_params,
)
from unillm.llm.vllm import VLLMHandler
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
