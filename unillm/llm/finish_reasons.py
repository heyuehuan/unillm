"""
Translating Gemini's finish reasons into OpenAI's.

The two vocabularies do not line up: Gemini names several distinct ways a
response can be cut short, while OpenAI has "stop", "length" and
"content_filter". Everything that is a refusal maps onto content_filter — a
client that only checks for "stop" must not be told a blocked response ended
normally, which is what a bare `.get(reason, "stop")` did for RECITATION,
BLOCKLIST, PROHIBITED_CONTENT and SPII.

Two entry points because the two Vertex handlers see different types: the REST
API sends the reason as a name, and the SDK hands back a proto enum that behaves
like an int.
"""

from typing import Optional

from unillm._logging import verbose_proxy_logger

# Gemini's FinishReason names to OpenAI's finish_reason values.
_TO_OPENAI = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
}

# The proto enum's numbering, for SDK responses that carry the raw value.
# FINISH_REASON_UNSPECIFIED (0) means the candidate is not finished yet.
_ENUM_NAMES = {
    1: "STOP",
    2: "MAX_TOKENS",
    3: "SAFETY",
    4: "RECITATION",
    5: "OTHER",
    6: "BLOCKLIST",
    7: "PROHIBITED_CONTENT",
    8: "SPII",
    9: "MALFORMED_FUNCTION_CALL",
}


def from_name(name: Optional[str]) -> Optional[str]:
    """Map a Gemini finish reason name. None (or absent) means "still generating"."""
    if not name:
        return None
    mapped = _TO_OPENAI.get(name)
    if mapped is None:
        # OTHER and MALFORMED_FUNCTION_CALL land here. "stop" is the only sensible
        # OpenAI value left, but the original is worth having in the log.
        verbose_proxy_logger.debug(f"Unmapped Gemini finish reason {name!r}; reporting 'stop'")
        return "stop"
    return mapped


def from_enum(value) -> Optional[str]:
    """
    Map a finish reason from the Vertex SDK, which may be a proto enum, its integer
    value, or already a name.
    """
    if value is None:
        return None
    name = getattr(value, "name", None)
    if name is None:
        if isinstance(value, str):
            name = value
        else:
            try:
                index = int(value)
            except (TypeError, ValueError):
                return None
            if index == 0:
                return None
            name = _ENUM_NAMES.get(index)
            if name is None:
                verbose_proxy_logger.debug(
                    f"Unknown Gemini finish reason value {index}; reporting 'stop'"
                )
                return "stop"
    if name in ("FINISH_REASON_UNSPECIFIED", ""):
        return None
    return from_name(name)
