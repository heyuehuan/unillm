"""
Security response headers.

The proxy serves an admin single-page app that keeps a JWT in localStorage, so a
cross-site scripting bug anywhere in the UI would be a full account takeover.
None of the usual browser-side defences were being sent. This module adds them.

Two Content-Security-Policy variants are needed. The app policy is strict:
scripts may only come from this origin, so injected inline script never runs. The
docs policy is looser because Swagger UI and ReDoc are loaded from a CDN and
bootstrap themselves with an inline script — that is FastAPI's design, and those
pages carry no session, so relaxing the policy there costs nothing.

Everything is overridable by environment variable, because a policy that breaks
an unusual deployment is worse than no policy at all if the only fix is a fork.
"""

import os
from typing import Dict, Optional

from starlette.requests import Request
from starlette.responses import Response

# Paths that render the interactive API docs. They need the relaxed policy.
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})

# Strict policy for the app and the API. No inline or third-party script at all.
# 'unsafe-inline' is present for styles only: React sets inline style attributes
# throughout the UI, and an injected style cannot execute code.
_APP_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'"
)

# Swagger UI and ReDoc: CDN assets, an inline bootstrap script, and a blob: worker
# for ReDoc's renderer.
_DOCS_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "worker-src 'self' blob:; "
    "connect-src 'self'"
)

_STATIC_HEADERS: Dict[str, str] = {
    # Stop the browser guessing a content type — the classic way a user-supplied
    # file gets executed as script.
    "X-Content-Type-Options": "nosniff",
    # frame-ancestors in the CSP covers modern browsers; this covers the rest.
    "X-Frame-Options": "DENY",
    # Never leak an admin URL (which carries project and key ids) to another site.
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
}


def _enabled() -> bool:
    return os.getenv("UNILLM_SECURITY_HEADERS", "true").strip().lower() not in ("false", "0", "no", "off")


def _app_csp() -> Optional[str]:
    """
    The policy for app and API responses.

    UNILLM_CSP replaces it outright; set it to an empty string (or "off") to send
    no policy, for a deployment that needs to embed or extend the UI.
    """
    override = os.getenv("UNILLM_CSP")
    if override is None:
        return _APP_CSP
    override = override.strip()
    if not override or override.lower() in ("off", "false", "none"):
        return None
    return override


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    # Behind a TLS-terminating proxy the scheme is http here. Trust the forwarded
    # header only under the same opt-in that governs forwarded client IPs.
    if os.getenv("UNILLM_TRUST_PROXY_HEADERS", "").strip().lower() == "true":
        return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    return False


def _hsts_value() -> Optional[str]:
    """HSTS header value, or None when disabled (UNILLM_HSTS_MAX_AGE=0)."""
    try:
        max_age = int(os.getenv("UNILLM_HSTS_MAX_AGE", "31536000").strip() or 0)
    except ValueError:
        max_age = 31536000
    if max_age <= 0:
        return None
    return f"max-age={max_age}; includeSubDomains"


def apply_security_headers(request: Request, response: Response) -> Response:
    """
    Add the security headers to `response`.

    Existing headers are left alone so a route can opt out of a specific one.
    HSTS is only sent over HTTPS: sending it over plain HTTP is ignored by
    browsers anyway, and omitting it keeps local HTTP development from pinning
    localhost to HTTPS in the developer's browser for a year.
    """
    if not _enabled():
        return response

    for name, value in _STATIC_HEADERS.items():
        response.headers.setdefault(name, value)

    csp = _DOCS_CSP if request.url.path in DOCS_PATHS else _app_csp()
    if csp:
        response.headers.setdefault("Content-Security-Policy", csp)

    if _is_https(request):
        hsts = _hsts_value()
        if hsts:
            response.headers.setdefault("Strict-Transport-Security", hsts)

    return response
