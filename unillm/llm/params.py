"""
Optional-parameter capability negotiation.

Modeled on how litellm resolves optional params: every provider config declares
``get_supported_openai_params(model)``, and ``get_optional_params()`` compares the
caller's non-default params against that list — raising ``UnsupportedParamsError``
unless ``drop_params`` is set, in which case the offending params are stripped and
the request proceeds.

UniLLM keeps those semantics but adds a second gate: a backend being *capable* of a
param is not enough, the model's config entry must also opt in. The split exists
because provider support is per-model and moves between releases. On Vertex AI,
gemini-2.0/2.5-flash serve logprobs while gemini-3.x reject them outright with
"Logprobs is not supported for this model". litellm declares logprobs supported for
every Gemini model and consequently 400s on the newer ones (BerriAI/litellm#31600);
a hardcoded allowlist here would rot the same way. Code says what a backend *could*
do; config says which models actually do it.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Set

from unillm._logging import verbose_proxy_logger


# Params that make up logprobs support, as one unit. A backend that can return
# chosen-token logprobs can always also return top-k alternatives, so there is no
# case where enabling one without the other is meaningful.
CHAT_LOGPROB_PARAMS = frozenset({"logprobs", "top_logprobs"})

# Text completions use OpenAI's legacy shape, where `logprobs` is an int (how many
# alternatives) rather than a bool, and there is no `top_logprobs`.
TEXT_LOGPROB_PARAMS = frozenset({"logprobs"})

# Config key on a model's unillm_params that opts it into logprobs.
SUPPORTS_LOGPROBS_KEY = "supports_logprobs"


class UnsupportedParamsError(Exception):
    """
    A client asked for params the resolved model does not serve.

    Carries the offending param names so the proxy can turn this into a 400 without
    re-deriving them. Mirrors litellm.UnsupportedParamsError, which is likewise a
    400-class error: asking for something the model cannot do is a bad request, not
    an upstream failure.
    """

    def __init__(self, message: str, params: List[str], model: str, provider: str):
        super().__init__(message)
        self.message = message
        self.params = params
        self.model = model
        self.provider = provider


def enabled_optional_params(model_params: Mapping[str, Any], *, text: bool = False) -> Set[str]:
    """
    The optional params a model's config entry opts into.

    Opt-in rather than opt-out: an unset `supports_logprobs` means the model does not
    serve logprobs. Defaulting to on would make every newly configured model a
    candidate for an upstream 400 that UniLLM could have answered itself.
    """
    if not _is_truthy(model_params.get(SUPPORTS_LOGPROBS_KEY)):
        return set()
    return set(TEXT_LOGPROB_PARAMS if text else CHAT_LOGPROB_PARAMS)


def resolve_optional_params(
    requested: Mapping[str, Any],
    *,
    capable: Iterable[str],
    enabled: Iterable[str],
    model: str,
    provider: str,
    drop_params: bool,
) -> Dict[str, Any]:
    """
    Filter `requested` down to what this model will actually serve.

    `requested` holds only params the client explicitly set (litellm's
    "non_default_params") — a param the caller never sent is not a param we can fail
    on. `capable` is what the handler can express against its backend at all;
    `enabled` is what config opted this model into. A param must clear both.

    Raises UnsupportedParamsError unless `drop_params` is set, matching litellm's
    default of failing loudly. Silently dropping is the more dangerous default here:
    a caller doing token-level analysis gets a 200 with no logprobs and no signal
    that their request was not honored.
    """
    capable, enabled = set(capable), set(enabled)
    allowed = capable & enabled

    # Split by *why* each param was rejected so the error names the actual remedy —
    # "turn it on in config" and "this backend can't do it" need different fixes.
    not_enabled = sorted(k for k in requested if k in capable and k not in allowed)
    not_capable = sorted(k for k in requested if k not in capable)

    if not_enabled or not_capable:
        if not drop_params:
            raise UnsupportedParamsError(
                message=_unsupported_message(not_capable, not_enabled, model, provider),
                params=sorted(not_capable + not_enabled),
                model=model,
                provider=provider,
            )
        verbose_proxy_logger.warning(
            f"Dropping unsupported params {sorted(not_capable + not_enabled)} for "
            f"model={model} provider={provider} (drop_params is enabled)"
        )

    return {k: v for k, v in requested.items() if k in allowed}


def _unsupported_message(
    not_capable: List[str], not_enabled: List[str], model: str, provider: str
) -> str:
    parts = []
    if not_capable:
        parts.append(
            f"{provider} does not support parameters: {not_capable}, for model={model}"
        )
    if not_enabled:
        parts.append(
            f"model={model} is not configured for parameters: {not_enabled}. Set "
            f"`{SUPPORTS_LOGPROBS_KEY}: true` in that model's unillm_params to enable "
            f"them (confirm the backend model serves logprobs first)"
        )
    parts.append(
        "To drop unsupported params instead of failing, set `drop_params: true` under "
        "general_settings."
    )
    return ". ".join(parts)


def _is_truthy(value: Any) -> bool:
    """Accept YAML booleans and the string forms a hand-edited config tends to grow."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)
