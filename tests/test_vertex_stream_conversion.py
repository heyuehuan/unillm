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
