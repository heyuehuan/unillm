"""
One timezone story for every timestamp the console shows.

Timestamps are stored naive-UTC — every `created_at` in the database is an instant
with the offset stripped off. That is fine on disk and correct in SQL, but it is a
lie in JSON: `"2026-09-02T13:19:54"` names no instant at all, and a browser reading
it applies its own offset. In Toronto that put every event four hours in the future,
so the console showed "just now" for things that had happened hours ago — and for
two different events at once, which is what made it obvious.

Two separate problems live here, and they are fixed separately:

  * **What instant was it?** The API has to say so unambiguously. `UtcDatetime`
    marks a response field as naive-UTC and serializes it with a `Z`, so every
    client — browser, curl, a script — reads the same instant.

  * **What should it read as?** That is a display choice, and it belongs to the
    deployment rather than to whichever laptop happens to be looking. A team in one
    office comparing a log line to a Slack message wants one answer, not one per
    timezone. `display_timezone()` is that choice, and the console formats every
    timestamp in it.

The default is `America/Toronto`, which is where this deployment's operators are.
It is an IANA zone name rather than "EST" on purpose: the zone follows the region
through daylight saving, so summer timestamps read as EDT without anyone editing
config twice a year. A fixed "EST" would be an hour wrong for eight months.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import PlainSerializer

from unillm._logging import verbose_proxy_logger

DEFAULT_DISPLAY_TIMEZONE = "America/Toronto"


def as_utc(value: datetime) -> datetime:
    """
    Attach UTC to a naive timestamp, or convert an aware one.

    Naive means "UTC with the label lost" everywhere in this codebase, so labelling
    it is a correction, not an assumption.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 in UTC, ending in `Z`. `None` passes through."""
    if value is None:
        return None
    # `isoformat()` writes "+00:00"; `Z` says the same thing and is what every
    # client that reads these logs expects to see.
    return as_utc(value).isoformat().replace("+00:00", "Z")


# Response-model annotation. Use it instead of a bare `datetime` on anything that
# leaves the server, so the offset survives the trip.
UtcDatetime = Annotated[
    datetime, PlainSerializer(iso_utc, return_type=str, when_used="json-unless-none")
]


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def display_timezone() -> str:
    """
    The IANA zone the console formats timestamps in.

    A misspelled zone falls back to the default with a warning rather than raising.
    Getting this wrong should mean the times read oddly, not that the server refuses
    to start or that a log page 500s.
    """
    from unillm.proxy.auth import get_general_settings

    configured = (get_general_settings() or {}).get("display_timezone")
    if configured is None:
        return DEFAULT_DISPLAY_TIMEZONE
    name = str(configured).strip()
    if not name:
        return DEFAULT_DISPLAY_TIMEZONE
    if not is_valid_timezone(name):
        verbose_proxy_logger.warning(
            "Unknown display_timezone %r in general_settings; falling back to %s. "
            "It has to be an IANA zone name such as America/Toronto or UTC.",
            name, DEFAULT_DISPLAY_TIMEZONE,
        )
        return DEFAULT_DISPLAY_TIMEZONE
    return name
