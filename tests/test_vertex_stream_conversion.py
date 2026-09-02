"""
/v1/completions on Vertex is a chat call reshaped into the legacy text format.
The reshaping happens chunk by chunk while streaming, and anything the converter
forgets to copy is gone for good — including the token counts the proxy bills and
logs from.
"""
import asyncio
import json

import pytest

from unillm.llm.vertex_ai import VertexAIHandler
from unillm.llm.vertex_ai_kms import VertexAIKMSHandler

HANDLERS = [VertexAIHandler, VertexAIKMSHandler]


async def _collect(handler_cls, chat_chunks, model="gemini-2.5-flash"):
    async def source():
        for chunk in chat_chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    out = []
    async for raw in handler_cls()._convert_chat_stream_to_text_stream(source(), model):
        payload = raw[6:].strip()
        if payload != "[DONE]":
            out.append(json.loads(payload))
    return out


@pytest.mark.parametrize("handler_cls", HANDLERS)
def test_streamed_text_completions_keep_their_usage(handler_cls):
    """
    Vertex reports token counts on the final chunk. Dropping them left streamed
    /v1/completions logged as zero tokens and zero cost, and the caller never saw
    the usage block either.
    """
    chunks = asyncio.run(_collect(handler_cls, [
        {"id": "chatcmpl-abc", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {"id": "chatcmpl-abc", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
    ]))

    assert [c["choices"][0]["text"] for c in chunks] == ["Hello", ""]
    assert "usage" not in chunks[0]  # only where the backend actually sent it
    assert chunks[1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3,
                                  "total_tokens": 10}
    assert chunks[1]["choices"][0]["finish_reason"] == "stop"
    assert all(c["object"] == "text_completion" for c in chunks)
    assert all(c["id"] == "cmpl-abc" for c in chunks)


# ── candidate identity and finish reasons ──────────────────

from types import SimpleNamespace  # noqa: E402


def test_rest_stream_chunk_uses_the_candidates_own_index():
    """
    With n > 1 Vertex sends only the candidates that produced tokens in a given
    chunk, so numbering them by their position in the chunk relabels candidate 2 as
    candidate 0 and the client stitches two different completions together.
    """
    chunk = VertexAIHandler()._convert_stream_chunk({
        "candidates": [{"index": 2, "content": {"parts": [{"text": "second"}]}}]
    }, model="gemini-2.5-flash")
    assert chunk["choices"][0]["index"] == 2


def test_rest_stream_chunk_falls_back_to_position_without_an_index():
    chunk = VertexAIHandler()._convert_stream_chunk({
        "candidates": [{"content": {"parts": [{"text": "only"}]}}]
    }, model="gemini-2.5-flash")
    assert chunk["choices"][0]["index"] == 0


@pytest.mark.parametrize("reason,expected", [
    ("STOP", "stop"),
    ("MAX_TOKENS", "length"),
    ("SAFETY", "content_filter"),
    ("RECITATION", "content_filter"),
    ("PROHIBITED_CONTENT", "content_filter"),
    ("SPII", "content_filter"),
    ("BLOCKLIST", "content_filter"),
])
def test_rest_stream_reports_why_generation_stopped(reason, expected):
    """A blocked or truncated response must not reach the client looking like 'stop'."""
    chunk = VertexAIHandler()._convert_stream_chunk(
        {"candidates": [{"finishReason": reason, "content": {"parts": []}}]},
        model="gemini-2.5-flash",
    )
    assert chunk["choices"][0]["finish_reason"] == expected


def test_rest_stream_leaves_finish_reason_unset_while_generating():
    chunk = VertexAIHandler()._convert_stream_chunk(
        {"candidates": [{"content": {"parts": [{"text": "mid"}]}}]}, model="m")
    assert chunk["choices"][0]["finish_reason"] is None


# ── the KMS stream, which is assembled by hand ─────────────

def _part(text):
    return SimpleNamespace(text=text)


def _candidate(index=0, text=None, finish_reason=None, logprobs=None):
    content = SimpleNamespace(parts=[_part(text)]) if text is not None else None
    return SimpleNamespace(index=index, content=content,
                           finish_reason=finish_reason, logprobs_result=logprobs)


def _logprobs_result(token):
    return SimpleNamespace(
        chosen_candidates=[SimpleNamespace(token=token, log_probability=-0.5)],
        top_candidates=[],
    )


def _kms_stream(chunks):
    """Drive the real streaming bridge with a stand-in SDK response."""
    handler = VertexAIKMSHandler()
    model_obj = SimpleNamespace(generate_content=lambda *a, **kw: iter(chunks))

    async def drive():
        out = []
        async for raw in handler._stream_response(model_obj, [], {}, "gemini-2.5-flash"):
            payload = raw[6:].strip()
            if payload != "[DONE]":
                out.append(json.loads(payload))
        return out

    return asyncio.run(drive())


def test_kms_stream_keeps_candidates_apart():
    chunks = _kms_stream([
        SimpleNamespace(candidates=[_candidate(0, "first"), _candidate(1, "second")],
                        usage_metadata=None),
        SimpleNamespace(candidates=[_candidate(1, finish_reason=2),
                                    _candidate(0, finish_reason=1)],
                        usage_metadata=None),
    ])

    text = [(c["choices"][0]["index"], c["choices"][0]["delta"].get("content"))
            for c in chunks if c["choices"][0]["delta"].get("content")]
    assert text == [(0, "first"), (1, "second")]

    # One closing choice per candidate, each with the reason Vertex gave it. Before
    # the fix there was a single choice at index 0 hardcoded to "stop".
    closing = chunks[-1]["choices"]
    assert {c["index"]: c["finish_reason"] for c in closing} == {0: "stop", 1: "length"}


def test_kms_stream_reports_a_blocked_response_as_filtered():
    chunks = _kms_stream([
        SimpleNamespace(candidates=[_candidate(0, "partial")], usage_metadata=None),
        SimpleNamespace(candidates=[_candidate(0, finish_reason=3)], usage_metadata=None),
    ])
    assert chunks[-1]["choices"][0]["finish_reason"] == "content_filter"


def test_kms_stream_keeps_logprobs_from_a_chunk_with_no_text():
    """
    Vertex reports the last position's logprobs on the chunk that carries only the
    finish reason. Emitting nothing for a text-less candidate dropped them.
    """
    chunks = _kms_stream([
        SimpleNamespace(candidates=[_candidate(0, "hi", logprobs=_logprobs_result("hi"))],
                        usage_metadata=None),
        SimpleNamespace(candidates=[_candidate(0, finish_reason=1,
                                               logprobs=_logprobs_result("!"))],
                        usage_metadata=None),
    ])

    tokens = [c["choices"][0]["logprobs"]["content"][0]["token"]
              for c in chunks if "logprobs" in c["choices"][0]]
    assert tokens == ["hi", "!"]


def test_kms_stream_still_closes_a_response_that_produced_nothing():
    chunks = _kms_stream([SimpleNamespace(candidates=[], usage_metadata=None)])
    assert chunks[-1]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
