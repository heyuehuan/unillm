"""
Resolving the real client address when UniLLM sits behind a reverse proxy.

`X-Forwarded-For` is a list, and which element is the real client depends on how
many proxies you trust — not on where the element sits. Each proxy appends the
address it received the request from, so the list reads:

    <whatever the client sent>, <client as seen by proxy 1>, ..., <proxy N-1 as seen by proxy N>

Only the trailing entries were written by infrastructure we control. The leading
entries are attacker-controlled: a client can send any `X-Forwarded-For` it likes
and every proxy in the chain will preserve it. Reading the *first* element — the
obvious-looking "original client" — therefore hands the client its own identity,
which defeats per-IP rate limiting and forges the IP recorded in the audit trail.

So we count from the right instead. With `UNILLM_TRUSTED_PROXY_HOPS` proxies in
front, the client address is the Nth entry from the end. If the header is shorter
than the configured chain, it did not come through the expected path and nothing
in it is trustworthy, so we fall back to the peer address.

None of this applies unless `UNILLM_TRUST_PROXY_HEADERS=true`. Without a proxy
guaranteeing the trailing entries, the whole header is client-written.
"""

import os
from typing import List, Optional

from fastapi import Request

TRUST_ENV = "UNILLM_TRUST_PROXY_HEADERS"
HOPS_ENV = "UNILLM_TRUSTED_PROXY_HOPS"

_DEFAULT_HOPS = 1

# The "you are behind a proxy but did not say so" warning is worth saying once,
# not once per request.
_warned_untrusted_xff = False


def trust_proxy_headers() -> bool:
    return os.getenv(TRUST_ENV, "").strip().lower() == "true"


def trusted_proxy_hops() -> int:
    """
    Number of reverse proxies in front of UniLLM. Defaults to 1, the usual
    single-nginx deployment. A bad value falls back to the default rather than
    failing startup, matching how the rate-limit specs are parsed.
    """
    raw = os.getenv(HOPS_ENV, "").strip()
    if not raw:
        return _DEFAULT_HOPS
    try:
        hops = int(raw)
        if hops < 1:
            raise ValueError
        return hops
    except ValueError:
        from unillm._logging import verbose_proxy_logger
        verbose_proxy_logger.warning(
            f"Ignoring malformed {HOPS_ENV}={raw!r}; expected a positive integer. "
            f"Using {_DEFAULT_HOPS}."
        )
        return _DEFAULT_HOPS


def _header_list(request: Request, header: str) -> List[str]:
    raw = request.headers.get(header)
    if not raw:
        return []
    return [value.strip() for value in raw.split(",") if value.strip()]


def _trusted_entry(request: Request, header: str) -> Optional[str]:
    """
    The entry written by the outermost proxy we trust, or None when the header is
    absent or shorter than the trusted chain.
    """
    values = _header_list(request, header)
    hops = trusted_proxy_hops()
    if len(values) < hops:
        return None
    return values[-hops]


def _warn_if_proxied_without_trust(request: Request) -> None:
    """
    Warn once when requests arrive carrying `X-Forwarded-For` but forwarded headers
    are not trusted. That combination means every client is collapsed into the
    proxy's single address, so per-IP rate limits apply to the whole deployment at
    once and one attacker can throttle everybody.
    """
    global _warned_untrusted_xff
    if _warned_untrusted_xff or "x-forwarded-for" not in request.headers:
        return
    _warned_untrusted_xff = True
    from unillm._logging import verbose_proxy_logger
    verbose_proxy_logger.warning(
        "Requests carry X-Forwarded-For but %s is not enabled, so every client "
        "shares one rate-limit bucket and audit logs record the proxy's address. "
        "Set %s=true (and %s to the number of proxies in front) if this really is "
        "a trusted reverse proxy.",
        TRUST_ENV, TRUST_ENV, HOPS_ENV,
    )


def client_ip(request: Request) -> Optional[str]:
    """Address of the client, honoring forwarded headers only when trusted."""
    peer = request.client.host if request.client else None
    if not trust_proxy_headers():
        _warn_if_proxied_without_trust(request)
        return peer
    return _trusted_entry(request, "x-forwarded-for") or peer


def forwarded_proto_is_https(request: Request) -> bool:
    """Whether the client's own connection used TLS, per the trusted proxy."""
    if not trust_proxy_headers():
        return False
    entry = _trusted_entry(request, "x-forwarded-proto")
    return entry is not None and entry.lower() == "https"


def reset_warning_state() -> None:
    """Forget the once-only warning (test helper)."""
    global _warned_untrusted_xff
    _warned_untrusted_xff = False
