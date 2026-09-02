"""
Response-side logprob thinning.

`top_logprobs` fixes the number of alternatives per position, so asking for 20 pays for
20 at *every* generated token — including the many positions where the model was nearly
certain and ranks 3 through 20 are noise many orders of magnitude below the winner.
`logprobs_min_p` drops alternatives whose probability falls under a floor, which trims
those confident positions while leaving genuinely uncertain ones intact.

This is a filter over what the backend already returned, never a request parameter, for
two reasons.

The wire format has no way to express it. OpenAI-compatible backends take a top-k count
and nothing else, so "give me everything above p" is not a question the proxy can ask.

And a bare threshold is not a safe thing to ask for. Probabilities sum to 1, so at most
1/p tokens can exceed a probability of p: a floor of 0.0001 permits up to 10,000
alternatives at a single position, where `top_logprobs` caps at 20. Used alone a low
floor is an unbounded expansion, not a reduction. Layered over top-k it can only ever
remove entries, which is why it is applied here rather than forwarded.

The chosen token's own `logprob` is never filtered. A sampled token can legitimately sit
far down the tail, and dropping it would discard the one value every caller needs.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from unillm.types import ChoiceLogprobs


def min_logprob_for(min_p: Optional[float]) -> Optional[float]:
    """
    Convert a probability floor into a log-probability floor, or None for no filtering.

    Comparing in log space keeps this to one `math.log` per request instead of an
    `exp()` per alternative, and avoids the precision loss of exponentiating values
    that are already logs.

    A floor of 0 admits every token — nothing has negative probability — so it is
    treated as "no filter" rather than passed to `math.log`, which would raise.
    """
    if min_p is None or min_p <= 0.0:
        return None
    return math.log(min_p)


def filter_choice_logprobs(
    logprobs: Optional[ChoiceLogprobs], min_logprob: Optional[float]
) -> Optional[ChoiceLogprobs]:
    """Thin the alternatives in a parsed chat-shaped ChoiceLogprobs, in place."""
    if logprobs is None or min_logprob is None or not logprobs.content:
        return logprobs
    for entry in logprobs.content:
        if entry.top_logprobs:
            entry.top_logprobs = [
                alt for alt in entry.top_logprobs if alt.logprob >= min_logprob
            ]
    return logprobs


def filter_chat_logprobs_payload(
    logprobs: Optional[Dict[str, Any]], min_logprob: Optional[float]
) -> Optional[Dict[str, Any]]:
    """
    Thin a chat-shaped logprobs object that is still a plain dict.

    Used on the streaming path, where chunks are JSON rather than parsed models.
    """
    if not logprobs or min_logprob is None:
        return logprobs
    content = logprobs.get("content")
    if not content:
        return logprobs
    for entry in content:
        alts = entry.get("top_logprobs")
        if alts:
            entry["top_logprobs"] = [
                alt for alt in alts if alt.get("logprob", 0.0) >= min_logprob
            ]
    return logprobs


def filter_legacy_logprobs(
    logprobs: Optional[Dict[str, Any]], min_logprob: Optional[float]
) -> Optional[Dict[str, Any]]:
    """
    Thin the alternatives in the legacy `/v1/completions` logprobs shape.

    That shape is flat and differently organised: `top_logprobs` is a list of
    {token: logprob} maps, one per position, while the chosen tokens live in a
    parallel `token_logprobs` array that is left alone.
    """
    if not logprobs or min_logprob is None:
        return logprobs
    per_position = logprobs.get("top_logprobs")
    if not per_position:
        return logprobs
    thinned: List[Any] = []
    for alts in per_position:
        if isinstance(alts, dict):
            thinned.append({t: lp for t, lp in alts.items() if lp >= min_logprob})
        else:
            # Unknown shape from a backend that does not follow the legacy format —
            # pass it through rather than dropping data we cannot interpret.
            thinned.append(alts)
    logprobs["top_logprobs"] = thinned
    return logprobs


def filter_stream_chunk(chunk: Dict[str, Any], min_logprob: Optional[float]) -> Dict[str, Any]:
    """
    Thin every choice in an already-parsed SSE chunk.

    Chat chunks carry `choices[].logprobs` in the nested shape; legacy text chunks
    carry the flat one. Both are keyed the same way, so the shape is detected per
    choice instead of being threaded down from the endpoint.
    """
    if min_logprob is None:
        return chunk
    for choice in chunk.get("choices") or []:
        logprobs = choice.get("logprobs")
        if not isinstance(logprobs, dict):
            continue
        if "content" in logprobs:
            filter_chat_logprobs_payload(logprobs, min_logprob)
        else:
            filter_legacy_logprobs(logprobs, min_logprob)
    return chunk
