"""
Work that touches the database must not run on the event loop.

FastAPI serves every request in this proxy from one loop, so a synchronous
database call inside an async path does not just slow that request down — it
stops every other in-flight request, including ones streaming tokens back to a
caller, for as long as the write takes. On a locked SQLite file that is seconds.

Each test drives the real code path and asserts the blocking part ran somewhere
other than the loop thread.
"""
import asyncio
import threading
import time

import pytest

from unillm.proxy import proxy_server, server_settings


@pytest.fixture(autouse=True)
def _cold_settings_cache():
    server_settings.invalidate_cache()
    yield
    server_settings.invalidate_cache()


async def _stays_responsive(coro):
    """
    Run `coro` next to a ticker and report both the result and how many times the
    loop got a turn. A blocking call inside `coro` freezes the ticker at zero.
    """
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    ticker = asyncio.ensure_future(tick())
    try:
        result = await coro
    finally:
        ticker.cancel()
    return result, ticks


def test_a_cold_settings_read_does_not_freeze_the_loop(monkeypatch):
    loop_thread = None
    read_thread = None

    def slow_read(key):
        nonlocal read_thread
        read_thread = threading.get_ident()
        time.sleep(0.1)  # stands in for a contended database
        return 4242

    monkeypatch.setattr(server_settings, "get_setting_cached", slow_read)

    async def main():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        return await _stays_responsive(
            server_settings.get_setting_cached_async(server_settings.LOGPROBS_MAX_BYTES))

    (value, ticks) = asyncio.run(main())
    assert value == 4242
    assert read_thread != loop_thread
    assert ticks > 0, "the loop made no progress while the setting was being read"


def test_a_warm_settings_read_stays_inline(monkeypatch):
    """The warm path is a dict lookup; pushing it to a thread would be pure overhead."""
    monkeypatch.setattr(server_settings, "_cache", {server_settings.LOGPROBS_MAX_BYTES: 99})
    monkeypatch.setattr(server_settings, "_cache_expires_at", time.monotonic() + 60)

    def fail(key):  # pragma: no cover - must not be reached
        raise AssertionError("warm cache should not open a session")

    monkeypatch.setattr(server_settings, "get_setting_cached", fail)
    assert asyncio.run(
        server_settings.get_setting_cached_async(server_settings.LOGPROBS_MAX_BYTES)) == 99


def test_writing_a_stream_log_does_not_freeze_the_loop(monkeypatch):
    """
    The request log for a streamed response is written in the generator's finally,
    which runs on the loop. Inline, that write blocked every other request just as
    the stream was finishing.
    """
    loop_thread = None
    write_thread = None

    def slow_write(**kwargs):
        nonlocal write_thread
        write_thread = threading.get_ident()
        time.sleep(0.1)

    monkeypatch.setattr(proxy_server, "_write_request_log", slow_write)

    async def upstream():
        yield 'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        yield "data: [DONE]\n\n"

    async def drain():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        chunks = []
        async for chunk in proxy_server._stream_with_logging(
                upstream(), "gpt-4", {}, time.time()):
            chunks.append(chunk)
        return chunks

    async def main():
        return await _stays_responsive(drain())

    (chunks, ticks) = asyncio.run(main())
    assert any("hi" in c for c in chunks)
    assert write_thread is not None, "the log was never written"
    assert write_thread != loop_thread
    assert ticks > 0, "the loop made no progress while the log was being written"
