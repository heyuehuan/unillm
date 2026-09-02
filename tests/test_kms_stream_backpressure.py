"""
The KMS streaming bridge does not outrun its reader.

The Vertex SDK's streaming iterator is synchronous, so the handler runs it on a
thread and hands chunks to the async generator through a queue. That queue was
unbounded: the thread pulled the whole completion from Vertex as fast as it
arrived and held it in memory, however slowly the client was reading — once per
concurrent stream. A bounded queue parks the producer instead.
"""
import asyncio
import threading
import types

import pytest

from unillm.llm import vertex_ai_kms
from unillm.llm.vertex_ai_kms import VertexAIKMSHandler

TOTAL_CHUNKS = 2000


def _chunk(text="x"):
    candidate = types.SimpleNamespace(
        index=0, finish_reason=None, logprobs_result=None,
        content=types.SimpleNamespace(parts=[types.SimpleNamespace(text=text)]),
    )
    return types.SimpleNamespace(usage_metadata=None, candidates=[candidate])


class _CountingStream:
    """Yields chunks as fast as it is asked to, and remembers how many were taken."""

    def __init__(self, total):
        self.total = total
        self.produced = 0

    def __iter__(self):
        for _ in range(self.total):
            self.produced += 1
            yield _chunk()


def _model_with(stream):
    model = types.SimpleNamespace()
    model.generate_content = lambda *a, **k: iter(stream)
    return model


def _read_a_few_then_stop(handler, stream, count=20, delay=0.005):
    """
    Read `count` chunks at a client's pace, then walk away.

    The delay is what makes this a real reader: while the consumer waits, the event
    loop is free to keep servicing the producer, which is exactly when an unbounded
    queue fills up with the rest of the completion.
    """
    async def run():
        agen = handler._stream_response(_model_with(stream), [], {}, "gemini-2.5-flash")
        read = []
        async for item in agen:
            read.append(item)
            await asyncio.sleep(delay)
            if len(read) >= count:
                break
        await agen.aclose()
        return read
    return asyncio.run(run())


def test_a_slow_reader_does_not_buffer_the_whole_completion():
    stream = _CountingStream(TOTAL_CHUNKS)
    handler = VertexAIKMSHandler()

    read = _read_a_few_then_stop(handler, stream)

    assert len(read) == 20
    # Producing everything would mean the whole response sat in memory waiting for a
    # reader that had taken twenty chunks.
    assert stream.produced < TOTAL_CHUNKS // 4, stream.produced
    # Tighter: the queue depth, plus what the consumer had already drained from it.
    assert stream.produced <= vertex_ai_kms._STREAM_QUEUE_MAXSIZE + 32, stream.produced


def test_the_producer_thread_is_not_left_parked_on_a_full_queue():
    """
    Backpressure introduces a way to hang: the producer waits for queue space that a
    departed consumer will never make. Closing the stream has to release it.
    """
    stream = _CountingStream(TOTAL_CHUNKS)
    before = {t.ident for t in threading.enumerate()}

    _read_a_few_then_stop(VertexAIKMSHandler(), stream)

    leftovers = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert leftovers == [], leftovers


def test_a_short_stream_still_finishes_normally():
    """The bound must not change the ordinary case: every chunk reaches the client."""
    stream = _CountingStream(5)
    handler = VertexAIKMSHandler()

    async def run():
        return [item async for item in
                handler._stream_response(_model_with(stream), [], {}, "gemini-2.5-flash")]

    items = asyncio.run(run())
    # Five content chunks, then the closing chunk and the [DONE] marker.
    assert sum('"content"' in i for i in items) == 5
    assert items[-1] == "data: [DONE]\n\n"
