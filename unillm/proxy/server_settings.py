"""
Deployment settings that an admin can change without a restart.

Some knobs need two homes. The YAML file is the right place to state a deployment's
intent, because it is version-controlled and survives a rebuilt database. But an
operator who discovers at 2am that a limit is too tight should not have to edit a
file and bounce the proxy. So each setting here resolves in three steps:

    database row (set from the admin console)
      -> `general_settings` in the YAML config
        -> built-in default

A database row existing *is* the override. Clearing it in the console deletes the
row and the setting falls back to the file, which is why there is no separate
"inherit" value to store.

Values are read on the request hot path, so resolved settings are cached for a few
seconds rather than queried every time. The TTL is short enough that a console edit
takes effect while you are still looking at the page, and it also bounds staleness
for deployments running more than one worker process, where an in-process cache
invalidated on write would go stale in every *other* worker.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from unillm.db import crud
from unillm.db.database import SessionLocal

# One megabyte. Enough for roughly 540 generated tokens of OpenAI-shaped logprobs at
# top_logprobs=20, or about 1500 in the compact format.
DEFAULT_LOGPROBS_MAX_BYTES = 1_048_576

LOGPROBS_MAX_BYTES = "logprobs_max_bytes"


class SettingDef:
    """
    One tunable: how to find it, how to validate it, and what it means.

    `coerce` runs on every source, not just admin input, because a YAML file can
    say `logprobs_max_bytes: "2mb"` just as easily as an API caller can.
    """

    def __init__(
        self,
        key: str,
        default: Any,
        coerce: Callable[[Any], Any],
        description: str,
        unit: Optional[str] = None,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ):
        self.key = key
        self.default = default
        self.coerce = coerce
        self.description = description
        self.unit = unit
        self.minimum = minimum
        self.maximum = maximum

    def validate(self, value: Any) -> Any:
        coerced = self.coerce(value)
        if self.minimum is not None and coerced < self.minimum:
            raise ValueError(f"'{self.key}' must be at least {self.minimum}")
        if self.maximum is not None and coerced > self.maximum:
            raise ValueError(f"'{self.key}' must be at most {self.maximum}")
        return coerced


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected a number, got a boolean")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"expected a whole number, got {value!r}")


SETTINGS: Dict[str, SettingDef] = {
    LOGPROBS_MAX_BYTES: SettingDef(
        key=LOGPROBS_MAX_BYTES,
        default=DEFAULT_LOGPROBS_MAX_BYTES,
        coerce=_as_int,
        description=(
            "Maximum serialized size of the logprobs returned for a single request. "
            "Requests whose size can be predicted up front are rejected before "
            "inference; anything that exceeds the cap while streaming is truncated "
            "and marked."
        ),
        unit="bytes",
        # 1 KiB is enough for a handful of positions, so a smaller value would
        # effectively disable logprobs while looking like a limit.
        minimum=1024,
        maximum=256 * 1024 * 1024,
    ),
}

# Populated from the YAML `general_settings` block at config load.
_config_settings: Dict[str, Any] = {}

_CACHE_TTL_SECONDS = 5.0
_cache: Dict[str, Any] = {}
_cache_expires_at: float = 0.0


def set_config_settings(settings: Optional[Dict[str, Any]]) -> None:
    """Record the YAML `general_settings` block as the middle tier of resolution."""
    global _config_settings
    _config_settings = settings or {}
    invalidate_cache()


def invalidate_cache() -> None:
    global _cache, _cache_expires_at
    _cache = {}
    _cache_expires_at = 0.0


def config_value(key: str) -> Optional[Any]:
    """The YAML value for a setting, or None if the file does not mention it."""
    definition = SETTINGS[key]
    if key not in _config_settings:
        return None
    try:
        return definition.validate(_config_settings[key])
    except ValueError:
        # A bad value in the file should not take the proxy down at request time.
        # It is reported as an invalid config value by the settings API instead.
        return None


def _resolve_all(db: Session) -> Dict[str, Any]:
    overrides = crud.get_server_setting_values(db)
    resolved: Dict[str, Any] = {}
    for key, definition in SETTINGS.items():
        value = None
        if key in overrides:
            try:
                value = definition.validate(overrides[key])
            except ValueError:
                value = None
        if value is None:
            value = config_value(key)
        resolved[key] = definition.default if value is None else value
    return resolved


def get_setting(db: Session, key: str) -> Any:
    """The effective value of a setting, cached for a few seconds."""
    global _cache, _cache_expires_at
    now = time.monotonic()
    if not _cache or now >= _cache_expires_at:
        _cache = _resolve_all(db)
        _cache_expires_at = now + _CACHE_TTL_SECONDS
    return _cache.get(key, SETTINGS[key].default)


def describe(db: Session) -> List[Dict[str, Any]]:
    """
    Every setting with its effective value and where that value came from.

    The console needs the source to say "inherited from the config file" rather
    than showing a number that looks like someone typed it in.
    """
    overrides = crud.get_server_setting_values(db)
    rows: List[Dict[str, Any]] = []
    for key, definition in SETTINGS.items():
        override: Optional[Any] = None
        if key in overrides:
            try:
                override = definition.validate(overrides[key])
            except ValueError:
                override = None
        from_config = config_value(key)
        if override is not None:
            value, source = override, "database"
        elif from_config is not None:
            value, source = from_config, "config"
        else:
            value, source = definition.default, "default"
        rows.append({
            "key": key,
            "value": value,
            "source": source,
            "default": definition.default,
            "config_value": from_config,
            "description": definition.description,
            "unit": definition.unit,
            "minimum": definition.minimum,
            "maximum": definition.maximum,
        })
    return rows


def get_setting_cached(key: str) -> Any:
    """
    Effective value of a setting without the caller supplying a session.

    The proxy's completion path has no database dependency and should not grow one
    just to read a limit, so a session is opened only when the cache is cold.
    """
    global _cache, _cache_expires_at
    now = time.monotonic()
    if _cache and now < _cache_expires_at:
        return _cache.get(key, SETTINGS[key].default)
    db = SessionLocal()
    try:
        return get_setting(db, key)
    finally:
        db.close()
