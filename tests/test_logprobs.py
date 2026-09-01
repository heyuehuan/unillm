"""
Optional-parameter capability negotiation.
"""

import pytest

from unillm.llm.params import (
    UnsupportedParamsError,
    enabled_optional_params,
    resolve_optional_params,
)
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
