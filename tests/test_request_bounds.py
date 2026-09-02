"""
Numeric request parameters are bounded at the schema, so a bad value is a 422
rather than something the backend has to cope with.

`n` is the one that matters: it multiplies the work a single request costs, so
without a ceiling one request can ask for an arbitrary number of completions.
The penalties advertised a -2..2 range in their description without enforcing it.
"""
import pytest
from pydantic import ValidationError

from unillm.types import ChatCompletionRequest, CompletionRequest


def _chat(**kw):
    return ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}], **kw)


def _text(**kw):
    return CompletionRequest(model="m", prompt="hi", **kw)


@pytest.mark.parametrize("build", [_chat, _text])
@pytest.mark.parametrize("value", [0, -1, 129, 10_000])
def test_n_outside_the_documented_range_is_rejected(build, value):
    with pytest.raises(ValidationError):
        build(n=value)


@pytest.mark.parametrize("build", [_chat, _text])
def test_n_inside_the_range_is_accepted(build):
    assert build(n=1).n == 1
    assert build(n=128).n == 128
    assert build().n == 1


@pytest.mark.parametrize("build", [_chat, _text])
@pytest.mark.parametrize("field", ["presence_penalty", "frequency_penalty"])
def test_penalties_enforce_the_range_they_advertise(build, field):
    assert getattr(build(**{field: 2.0}), field) == 2.0
    assert getattr(build(**{field: -2.0}), field) == -2.0
    with pytest.raises(ValidationError):
        build(**{field: 2.5})
    with pytest.raises(ValidationError):
        build(**{field: -2.5})
