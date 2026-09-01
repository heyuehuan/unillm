"""
Logprobs request/response types.

Covers the one rule the request model settles on its own: top_logprobs is
meaningless without logprobs, so it is rejected rather than passed on.
"""

import pytest

from unillm.types import ChatCompletionRequest


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
