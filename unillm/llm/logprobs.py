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

import json
import math
from collections import deque
from typing import Any, Deque, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Compact response format
# ---------------------------------------------------------------------------

OPENAI_FORMAT = "openai"
COMPACT_FORMAT = "compact"


def _merge_alternative(alts: Dict[str, float], token: str, logprob: float) -> None:
    """
    Add one alternative to a {token: logprob} map, keeping the higher value on collision.

    Two different token ids can decode to the same string — a leading-space variant,
    a byte-fallback piece, or the same word from different merges. The map is keyed by
    the decoded string, so those collide. Keeping the maximum means the value answers
    "how likely was this text here", which is what a caller reading `{"yes": ..., "no": ...}`
    is actually asking. Summing would be the other defensible choice, but summing log
    probabilities requires exponentiating and renormalising, and would report a number
    no single token ever had.
    """
    existing = alts.get(token)
    if existing is None or logprob > existing:
        alts[token] = logprob


def compact_choice_logprobs(logprobs: Optional[ChoiceLogprobs]) -> Optional[ChoiceLogprobs]:
    """
    Rewrite parsed chat logprobs into the flat {token: logprob} form.

    The OpenAI chat shape spends four JSON keys and a nested object on every single
    alternative. At `top_logprobs: 20` that structure is about two thirds of the bytes
    on the wire, so collapsing each position to one map cuts the payload by roughly
    60-65% while losing only the `bytes` field.
    """
    if logprobs is None or logprobs.content is None:
        return logprobs
    tokens: List[str] = []
    token_logprobs: List[float] = []
    per_position: List[Dict[str, float]] = []
    for entry in logprobs.content:
        tokens.append(entry.token)
        token_logprobs.append(entry.logprob)
        alts: Dict[str, float] = {}
        for alt in entry.top_logprobs or []:
            _merge_alternative(alts, alt.token, alt.logprob)
        per_position.append(alts)
    return ChoiceLogprobs(
        format=COMPACT_FORMAT,
        tokens=tokens,
        token_logprobs=token_logprobs,
        top_logprobs=per_position,
    )


def compact_chat_payload(logprobs: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Same conversion for a chat logprobs object that is still a plain dict (streaming)."""
    if not logprobs:
        return logprobs
    content = logprobs.get("content")
    if content is None:
        return logprobs
    tokens: List[str] = []
    token_logprobs: List[float] = []
    per_position: List[Dict[str, float]] = []
    for entry in content:
        tokens.append(entry.get("token", ""))
        token_logprobs.append(entry.get("logprob", 0.0))
        alts: Dict[str, float] = {}
        for alt in entry.get("top_logprobs") or []:
            _merge_alternative(alts, alt.get("token", ""), alt.get("logprob", 0.0))
        per_position.append(alts)
    return {
        "format": COMPACT_FORMAT,
        "tokens": tokens,
        "token_logprobs": token_logprobs,
        "top_logprobs": per_position,
    }


def compact_stream_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert every chat-shaped choice in an SSE chunk to the compact form.

    Legacy text chunks are left alone: their logprobs are already flat
    {token: logprob} maps, so there is nothing to compact.
    """
    for choice in chunk.get("choices") or []:
        logprobs = choice.get("logprobs")
        if isinstance(logprobs, dict) and "content" in logprobs:
            choice["logprobs"] = compact_chat_payload(logprobs)
    return chunk


# ---------------------------------------------------------------------------
# Tail selection (logprobs_last_n)
# ---------------------------------------------------------------------------
#
# Callers who only want to know how confident the model was about the *end* of its
# answer - the classification token, the final word, the yes/no - still have to receive
# a logprob for every position the model produced. At `top_logprobs: 20` that is tens of
# kilobytes to read one number.
#
# No upstream API can express this. OpenAI, Gemini, vLLM, TGI, llama.cpp and Together
# all take a per-position top-k count and apply it to every position; the only related
# knob anywhere is Fireworks' `echo_last`, and that trims the *prompt* suffix, not the
# generation. So this is a response-side slice, like `logprobs_min_p` and
# `logprobs_format`: the tokens are generated and returned either way, and what changes
# is how many of them cross the wire.


def _tail(seq: Optional[List[Any]], last_n: int) -> Optional[List[Any]]:
    """The final `last_n` elements, or the list unchanged if it is already shorter."""
    if seq is None:
        return None
    return seq[-last_n:] if last_n < len(seq) else seq


def last_n_choice_logprobs(
    logprobs: Optional[ChoiceLogprobs], last_n: Optional[int]
) -> Optional[ChoiceLogprobs]:
    """
    Keep only the final `last_n` positions of a parsed chat-shaped ChoiceLogprobs.

    Applied before the compact conversion and before the size budget, so both of those
    see the window that actually goes out rather than the full sequence.
    """
    if logprobs is None or not last_n or last_n <= 0:
        return logprobs
    if logprobs.content is not None:
        logprobs.content = _tail(logprobs.content, last_n)
    elif logprobs.tokens is not None:
        logprobs.tokens = _tail(logprobs.tokens, last_n)
        logprobs.token_logprobs = _tail(logprobs.token_logprobs, last_n)
        logprobs.top_logprobs = _tail(logprobs.top_logprobs, last_n)
    return logprobs


def last_n_logprobs_payload(
    logprobs: Optional[Dict[str, Any]], last_n: Optional[int]
) -> Optional[Dict[str, Any]]:
    """
    Same slice for a dict-shaped logprobs object: chat, compact, or legacy flat.

    `text_offset` is sliced alongside the tokens it indexes. Its values are absolute
    offsets into the completion text, so they stay meaningful after the head is dropped
    and a caller can still locate the window in the returned string.
    """
    if not logprobs or not last_n or last_n <= 0:
        return logprobs
    if logprobs.get("content") is not None:
        logprobs["content"] = _tail(logprobs["content"], last_n)
        return logprobs
    if logprobs.get("tokens") is not None:
        for key in ("tokens", "token_logprobs", "top_logprobs", "text_offset"):
            if logprobs.get(key) is not None:
                logprobs[key] = _tail(logprobs[key], last_n)
    return logprobs


class LastNWindow:
    """
    A rolling window over a streamed response's logprobs.

    Streaming makes this harder than the buffered case: which positions are the last N
    is not knowable until the stream ends, so the logprobs cannot be forwarded as they
    arrive. They are stripped from each chunk, kept in a per-choice window of at most N
    entries, and emitted once at the end in a final chunk.

    Only the logprobs are deferred. Content deltas still stream live, so the caller sees
    text at the same latency as any other request and simply receives the logprobs with
    the last chunk instead of spread across all of them.
    """

    def __init__(self, last_n: int):
        self.last_n = last_n
        # Per choice index, because `n: 4` streams four interleaved choices.
        self._chat: Dict[int, Deque[Dict[str, Any]]] = {}
        self._flat: Dict[int, Deque[tuple]] = {}
        self._flat_has_offset: Dict[int, bool] = {}
        # The identity fields of the last chunk seen, reused so the emitted chunk is
        # recognisably part of the same response rather than a synthetic object.
        self._template: Dict[str, Any] = {}

    def _window(self, store: Dict[int, Deque], index: int) -> Deque:
        if index not in store:
            store[index] = deque(maxlen=self.last_n)
        return store[index]

    def capture(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Buffer this chunk's logprob positions and remove them from the chunk."""
        for key in ("id", "object", "created", "model"):
            if key in chunk:
                self._template[key] = chunk[key]
        for choice in chunk.get("choices") or []:
            logprobs = choice.get("logprobs")
            if not isinstance(logprobs, dict):
                continue
            index = choice.get("index", 0)
            content = logprobs.get("content")
            if content is not None:
                self._window(self._chat, index).extend(content)
            elif logprobs.get("tokens") is not None:
                tokens = logprobs.get("tokens") or []
                token_logprobs = logprobs.get("token_logprobs") or []
                per_position = logprobs.get("top_logprobs") or []
                offsets = logprobs.get("text_offset")
                if offsets is not None:
                    self._flat_has_offset[index] = True
                window = self._window(self._flat, index)
                for i, token in enumerate(tokens):
                    window.append((
                        token,
                        token_logprobs[i] if i < len(token_logprobs) else 0.0,
                        per_position[i] if i < len(per_position) else {},
                        offsets[i] if offsets is not None and i < len(offsets) else None,
                    ))
            # Nulled rather than deleted: a `logprobs` key that disappears mid-stream
            # and reappears at the end reads as a malformed response to a client that
            # checks for its presence.
            choice["logprobs"] = None
        return chunk

    def _payload_for(self, index: int) -> Optional[Dict[str, Any]]:
        if index in self._chat:
            return {"content": list(self._chat[index])}
        if index in self._flat:
            rows = list(self._flat[index])
            payload = {
                "tokens": [r[0] for r in rows],
                "token_logprobs": [r[1] for r in rows],
                "top_logprobs": [r[2] for r in rows],
            }
            if self._flat_has_offset.get(index):
                payload["text_offset"] = [r[3] for r in rows]
            return payload
        return None

    def flush(self) -> Optional[Dict[str, Any]]:
        """
        Build the single trailing chunk carrying the buffered windows.

        Returns None when nothing was buffered, which is the normal case for a model
        that served no logprobs at all - there is nothing to say, so no chunk is added.
        """
        indexes = sorted(set(self._chat) | set(self._flat))
        if not indexes:
            return None
        is_text = self._template.get("object") == "text_completion"
        choices = []
        for index in indexes:
            choice: Dict[str, Any] = {"index": index, "finish_reason": None,
                                      "logprobs": self._payload_for(index)}
            # An empty delta/text keeps the chunk a valid member of its stream, so a
            # client accumulating the response concatenates nothing extra.
            if is_text:
                choice["text"] = ""
            else:
                choice["delta"] = {}
            choices.append(choice)
        return {**self._template, "choices": choices}


# ---------------------------------------------------------------------------
# Size budget
# ---------------------------------------------------------------------------

# Smallest number of bytes one generated position can serialize to, per format. These
# are lower bounds, not averages: a single-character token, a short logprob, and no
# `bytes` array. They exist only so a request can be rejected before inference when
# even the most favourable possible output would blow the budget. Using an average
# here would reject requests that were going to fit.
# The absolute smallest each shape can be is an empty token and a single-digit
# logprob, so these are set just under that: {"token":"","logprob":0,"top_logprobs":[]}
# is 42 bytes and {"token":"","logprob":0} is 24. Erring low only costs pre-flight
# rejections that truncation would have caught anyway; erring high would reject
# requests that were going to fit.
_MIN_BYTES_PER_POSITION = {
    OPENAI_FORMAT: 40,
    COMPACT_FORMAT: 4,
}
_MIN_BYTES_PER_ALTERNATIVE = {
    OPENAI_FORMAT: 24,
    COMPACT_FORMAT: 4,
}


def bounded_positions(max_tokens: Optional[int], last_n: Optional[int]) -> Optional[int]:
    """
    The most logprob positions this request can return, or None if it is unbounded.

    `max_tokens` bounds how many the model may generate; `logprobs_last_n` bounds how
    many of those are returned. Either one alone is a bound, and when both are set the
    smaller wins. This is why `logprobs_last_n` makes a request estimable even without
    `max_tokens`: however long the generation runs, only N positions come back.
    """
    candidates = [v for v in (max_tokens, last_n) if v and v > 0]
    return min(candidates) if candidates else None


def min_bytes_estimate(max_tokens: Optional[int], top_logprobs: Optional[int],
                       response_format: str, last_n: Optional[int] = None) -> Optional[int]:
    """
    A floor on how many bytes the logprobs for this request could occupy.

    Returns None when nothing bounds the number of positions, because then there is no
    honest estimate to make. Those requests are caught after the fact by truncation
    instead.
    """
    positions = bounded_positions(max_tokens, last_n)
    if positions is None:
        return None
    fmt = response_format if response_format in _MIN_BYTES_PER_POSITION else OPENAI_FORMAT
    per_alt = _MIN_BYTES_PER_ALTERNATIVE[fmt] * max(top_logprobs or 0, 0)
    return positions * (_MIN_BYTES_PER_POSITION[fmt] + per_alt)


def _encoded_size(value: Any) -> int:
    """Serialized byte length, using the separators FastAPI's JSONResponse uses."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class LogprobsBudget:
    """
    A per-request allowance for logprob bytes, spent across every choice and chunk.

    The cap is on the request, not on each choice, because `n: 4` returns four times
    the data down one connection. Positions are kept in generation order and dropped
    from the end once the allowance runs out, so a truncated response is a prefix of
    the full one rather than an arbitrary sample.

    Truncation is marked, never silent. A caller who scores 200 tokens and gets 140
    back needs to know the remaining 60 were withheld rather than assume the model
    stopped early.
    """

    def __init__(self, max_bytes: Optional[int]):
        self.max_bytes = max_bytes
        self.remaining = max_bytes if max_bytes is not None else None
        self.truncated = False

    @property
    def enabled(self) -> bool:
        return self.remaining is not None

    # A response's logprobs are not just the positions: there is the object around them
    # and a comma between each one. Ignoring that would let a response overshoot the
    # cap by a few percent, so both are charged. The envelope is charged per call,
    # which is once per choice when returning a whole response and once per chunk when
    # streaming — matching where the braces actually appear on the wire.
    _CHAT_ENVELOPE = len('{"content":[]}')
    _FLAT_ENVELOPE = len('{"format":"compact","tokens":[],"token_logprobs":[],"top_logprobs":[]}')
    # A chat position is one array element, so one comma. A flat position appears in
    # three parallel arrays, so three.
    _CHAT_SEPARATORS = 1
    _FLAT_SEPARATORS = 3
    # `,"truncated":true,"truncated_at":123456` — the marker is itself payload, and
    # whether it will be needed is only known after the last position is measured.
    # Reserving it up front is what keeps the served response under the cap rather
    # than a few dozen bytes over it.
    _MARKER_BYTES = len(',"truncated":true,"truncated_at":123456')

    def _charge_envelope(self, size: int) -> bool:
        """Spend the fixed cost of the logprobs object itself. False if it does not fit."""
        assert self.remaining is not None
        size += self._MARKER_BYTES
        if size > self.remaining:
            self.remaining = 0
            self.truncated = True
            return False
        self.remaining -= size
        return True

    def _take(self, sizes: List[int]) -> int:
        """How many leading positions fit in what is left, spending the budget on them."""
        assert self.remaining is not None
        kept = 0
        for size in sizes:
            if size > self.remaining:
                self.truncated = True
                break
            self.remaining -= size
            kept += 1
        return kept

    def apply_to_choice(self, logprobs: Optional[ChoiceLogprobs]) -> Optional[ChoiceLogprobs]:
        """Trim a parsed ChoiceLogprobs, in either format, to what the budget allows."""
        if not self.enabled or logprobs is None:
            return logprobs
        if logprobs.content is not None:
            if not self._charge_envelope(self._CHAT_ENVELOPE):
                logprobs.content = []
                logprobs.truncated = True
                logprobs.truncated_at = 0
                return logprobs
            # Not exclude_none: the response goes out as a plain model_dump(), so the
            # nested `"bytes":null` on every token and alternative is real payload.
            sizes = [_encoded_size(e.model_dump()) + self._CHAT_SEPARATORS
                     for e in logprobs.content]
            kept = self._take(sizes)
            if kept < len(logprobs.content):
                logprobs.content = logprobs.content[:kept]
                logprobs.truncated = True
                logprobs.truncated_at = kept
        elif logprobs.tokens is not None:
            if not self._charge_envelope(self._FLAT_ENVELOPE):
                logprobs.tokens, logprobs.token_logprobs, logprobs.top_logprobs = [], [], []
                logprobs.truncated = True
                logprobs.truncated_at = 0
                return logprobs
            sizes = self._compact_sizes(
                logprobs.tokens, logprobs.token_logprobs or [], logprobs.top_logprobs or []
            )
            kept = self._take(sizes)
            if kept < len(logprobs.tokens):
                logprobs.tokens = logprobs.tokens[:kept]
                logprobs.token_logprobs = (logprobs.token_logprobs or [])[:kept]
                logprobs.top_logprobs = (logprobs.top_logprobs or [])[:kept]
                logprobs.truncated = True
                logprobs.truncated_at = kept
        return logprobs

    @classmethod
    def _compact_sizes(cls, tokens: List[str], token_logprobs: List[Any], per_position: List[Any]) -> List[int]:
        sizes = []
        for i, token in enumerate(tokens):
            lp = token_logprobs[i] if i < len(token_logprobs) else 0.0
            alts = per_position[i] if i < len(per_position) else {}
            sizes.append(
                _encoded_size(token) + _encoded_size(lp) + _encoded_size(alts)
                + cls._FLAT_SEPARATORS
            )
        return sizes

    def apply_to_payload(self, logprobs: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Trim a dict-shaped logprobs object: chat, compact, or legacy text.

        Used on the streaming path, where a chunk carries only the positions produced
        since the last one. Spending from the same budget across chunks is what makes
        the cap apply to the whole response rather than to each chunk separately.
        """
        if not self.enabled or not logprobs:
            return logprobs
        content = logprobs.get("content")
        if content is not None:
            if not self._charge_envelope(self._CHAT_ENVELOPE):
                logprobs["content"] = []
                logprobs["truncated"] = True
                return logprobs
            kept = self._take([_encoded_size(e) + self._CHAT_SEPARATORS for e in content])
            if kept < len(content):
                logprobs["content"] = content[:kept]
                logprobs["truncated"] = True
        elif logprobs.get("tokens") is not None:
            tokens = logprobs.get("tokens") or []
            if not self._charge_envelope(self._FLAT_ENVELOPE):
                logprobs["tokens"], logprobs["token_logprobs"], logprobs["top_logprobs"] = [], [], []
                if logprobs.get("text_offset") is not None:
                    logprobs["text_offset"] = []
                logprobs["truncated"] = True
                return logprobs
            kept = self._take(self._compact_sizes(
                tokens, logprobs.get("token_logprobs") or [], logprobs.get("top_logprobs") or []
            ))
            if kept < len(tokens):
                logprobs["tokens"] = tokens[:kept]
                logprobs["token_logprobs"] = (logprobs.get("token_logprobs") or [])[:kept]
                logprobs["top_logprobs"] = (logprobs.get("top_logprobs") or [])[:kept]
                # Legacy completions carry a parallel offsets array that has to stay
                # the same length as the tokens it indexes.
                if logprobs.get("text_offset") is not None:
                    logprobs["text_offset"] = logprobs["text_offset"][:kept]
                logprobs["truncated"] = True
        return logprobs

    def apply_to_stream_chunk(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return chunk
        for choice in chunk.get("choices") or []:
            logprobs = choice.get("logprobs")
            if isinstance(logprobs, dict):
                self.apply_to_payload(logprobs)
        return chunk
