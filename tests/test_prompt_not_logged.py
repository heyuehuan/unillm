"""
Debug logs describe the request; they never quote it.

Running the proxy at debug level used to write the outgoing request body — the
caller's prompt, and the system instruction on the KMS path — into the log file.
Log files get shipped to a collector and kept for months, so that turned every
prompt into retained data nobody asked to retain, sitting outside the request-log
table with none of its access controls.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from unillm.llm.vertex_ai import VertexAIHandler
from unillm.llm.vertex_ai_kms import VertexAIKMSHandler

SECRET = "patient 4b12 has a suspected pulmonary embolism"
SYSTEM_SECRET = "you are the triage assistant for ward 9"

MESSAGES = [
    {"role": "system", "content": SYSTEM_SECRET},
    {"role": "user", "content": SECRET},
]


@pytest.fixture
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="unillm.proxy")
    return caplog


def _logged(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


def test_the_rest_handler_does_not_log_the_prompt(debug_logs):
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {},
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)

    handler = VertexAIHandler()
    handler._get_credentials_async = AsyncMock(return_value=(MagicMock(token="t"), "proj"))
    handler._get_http_client = AsyncMock(return_value=http_client)

    asyncio.run(handler.chat_completion(model="gemini-2.5-flash", messages=MESSAGES))

    # It really did send the prompt — it just did not write it down.
    sent = http_client.post.call_args.kwargs["json"]
    assert SECRET in str(sent)

    logged = _logged(debug_logs)
    assert SECRET not in logged
    assert SYSTEM_SECRET not in logged
    # Still useful for debugging: the shape of the request is there.
    assert "contents" in logged


def test_the_kms_handler_does_not_log_the_system_instruction(debug_logs, monkeypatch):
    handler = VertexAIKMSHandler()

    model = MagicMock()
    model.generate_content_async = AsyncMock(return_value=MagicMock(
        candidates=[], usage_metadata=None,
    ))
    monkeypatch.setattr(handler, "_get_model", lambda *a, **k: model)
    monkeypatch.setattr(handler, "_convert_response_to_openai",
                        lambda *a, **k: MagicMock())

    asyncio.run(handler.chat_completion(model="gemini-2.5-flash", messages=MESSAGES))

    logged = _logged(debug_logs)
    assert SYSTEM_SECRET not in logged
    assert SECRET not in logged
    assert "contents" in logged
