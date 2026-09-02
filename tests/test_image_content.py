"""
Image content in a chat message.

Both Vertex handlers take images inline, so the caller has to send a base64
data: URL. Two things used to go wrong. A data: URL without the `;base64,`
separator was split into two pieces that were not there, which raised ValueError
deep in the handler and reached the client as a 500 — an "our fault" status for
the caller's malformed input. And the KMS handler dropped image content on the
floor, so the model answered as though no image had been sent.
"""

import base64

import pytest

from unillm.llm.messages import InvalidRequestError, parse_image_data_url
from unillm.llm.vertex_ai import VertexAIHandler
from unillm.proxy.proxy_server import _sanitized_http_exception, _upstream_status

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n fake pixels").decode()
DATA_URL = f"data:image/png;base64,{PNG}"


def _image_message(url):
    return [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]


# --- a good data: URL still converts -------------------------------------------

def test_a_base64_data_url_becomes_an_inline_part():
    _system, contents = VertexAIHandler()._convert_messages_to_gemini_format(_image_message(DATA_URL))
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "what is this?"}
    assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": PNG}}


def test_the_parser_hands_back_both_encodings():
    image = parse_image_data_url({"url": DATA_URL})
    assert image.mime_type == "image/png"
    assert image.base64_data == PNG
    assert image.data == base64.b64decode(PNG)


# --- bad input is rejected, not crashed on ------------------------------------

@pytest.mark.parametrize("url", [
    "data:image/png,not-base64-at-all",        # no ;base64, separator — the old ValueError
    "data:image/png;base64,",                  # separator, no payload
    "data:;base64," + PNG,                     # no media type
    "data:image/png;base64,!!!not base64!!!",  # payload is not base64
    "https://example.com/cat.png",             # a link the backend will not fetch
    "",
])
def test_an_unusable_image_url_is_a_bad_request(url):
    with pytest.raises(InvalidRequestError):
        VertexAIHandler()._convert_messages_to_gemini_format(_image_message(url))


def test_a_missing_url_is_a_bad_request():
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]
    with pytest.raises(InvalidRequestError):
        VertexAIHandler()._convert_messages_to_gemini_format(messages)


def test_the_proxy_answers_it_as_a_400_and_logs_it_as_one():
    exc = InvalidRequestError("image_url data: URL is not valid base64")
    assert _upstream_status(exc) == 400

    http_exc = _sanitized_http_exception(exc)
    assert http_exc.status_code == 400
    # The message is ours, so the caller gets to see what was wrong with the input.
    assert "base64" in http_exc.detail


def test_a_link_is_refused_rather_than_silently_dropped():
    """
    The dangerous failure is not the error — it is answering anyway. A dropped
    image gets a confident answer about a picture the model never saw.
    """
    with pytest.raises(InvalidRequestError) as exc:
        parse_image_data_url({"url": "https://example.com/cat.png"})
    assert "does not fetch" in str(exc.value)


def test_the_kms_handler_sends_the_image_instead_of_dropping_it():
    from unillm.llm.vertex_ai_kms import VertexAIKMSHandler

    _system, contents = VertexAIKMSHandler()._convert_messages_to_contents(_image_message(DATA_URL))
    parts = contents[0].parts
    assert len(parts) == 2, "the text part survived and the image part was added"
    blob = parts[1].to_dict()["inline_data"]
    assert blob["mime_type"] == "image/png"
    assert base64.b64decode(blob["data"]) == base64.b64decode(PNG)
