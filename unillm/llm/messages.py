"""
Shared conversion of OpenAI message content into provider parts.

Only image content needs help so far. Both Vertex handlers take images inline,
which means the caller has to send a base64 data: URL — neither Gemini nor this
proxy fetches an http(s) image on the caller's behalf. Getting that wrong used to
be either a 500 (the data: URL was split on a separator that was not there) or,
worse, silence: the image was dropped and the model answered confidently about a
picture it had never seen.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, NamedTuple


class InvalidRequestError(ValueError):
    """
    A request UniLLM cannot convert for the chosen backend.

    Raised before anything is sent upstream, so the proxy answers it as a 400 of
    its own rather than reporting a provider failure.
    """


class InlineImage(NamedTuple):
    mime_type: str
    base64_data: str   # for handlers that speak the REST API
    data: bytes        # for handlers that speak the SDK


def parse_image_data_url(image_url: Any) -> InlineImage:
    """
    Read one OpenAI `image_url` content item as an inline image.

    Accepts `data:<mime>;base64,<data>` only, and rejects everything else with a
    message the caller can act on.
    """
    url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        raise InvalidRequestError("image_url is missing its 'url'")
    if not url.startswith("data:"):
        raise InvalidRequestError(
            "image_url must be a base64 data: URL — this backend does not fetch image links"
        )

    header, separator, payload = url.partition(";base64,")
    if not separator:
        raise InvalidRequestError(
            "image_url data: URL must be base64-encoded, as data:<media-type>;base64,<data>"
        )
    mime_type = header[len("data:"):]
    if not mime_type:
        raise InvalidRequestError("image_url data: URL is missing its media type")
    if not payload:
        raise InvalidRequestError("image_url data: URL carries no image data")

    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidRequestError("image_url data: URL is not valid base64") from None
    if not data:
        raise InvalidRequestError("image_url data: URL carries no image data")

    return InlineImage(mime_type=mime_type, base64_data=payload, data=data)
