"""
Security response headers.

The console keeps its JWT in localStorage and none of the browser-side defences
were being sent, so any future XSS bug in the UI would have been a full account
takeover with nothing standing in the way. These tests pin the headers, the split
between the strict app policy and the relaxed docs policy, and the escape hatches
a deployment needs if a policy gets in its way.
"""
import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app
from unillm.proxy import security_headers
from unillm.db.database import init_db, engine
from unillm.db.models import Base


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _csp_directives(value):
    """Parse a CSP header into {directive: [sources]}."""
    out = {}
    for part in value.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


# --- the headers are present -----------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
])
def test_static_headers_are_sent(client, header, expected):
    assert client.get("/health").headers[header] == expected


def test_permissions_policy_denies_device_access(client):
    value = client.get("/health").headers["Permissions-Policy"]
    for feature in ("geolocation", "microphone", "camera"):
        assert f"{feature}=()" in value


@pytest.mark.parametrize("path", ["/health", "/api/users/me", "/docs", "/openapi.json", "/no-such-page"])
def test_every_response_carries_a_policy(client, path):
    """Including error responses — a 401 or 404 body is still rendered by a browser."""
    r = client.get(path)
    assert "Content-Security-Policy" in r.headers, f"{path} -> {r.status_code}"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


# --- the app policy is actually strict -------------------------------------------

def test_app_policy_blocks_injected_script(client):
    """
    The whole point: script may only come from this origin, so an injected inline
    <script> or a remote payload cannot execute even if markup escaping fails.
    """
    csp = _csp_directives(client.get("/health").headers["Content-Security-Policy"])
    assert csp["script-src"] == ["'self'"]
    assert "'unsafe-inline'" not in csp["script-src"]
    assert "'unsafe-eval'" not in csp["script-src"]


def test_app_policy_locks_down_framing_and_plugins(client):
    csp = _csp_directives(client.get("/health").headers["Content-Security-Policy"])
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'self'"]      # stops a <base> tag redirecting relative URLs
    assert csp["form-action"] == ["'self'"]   # stops credential posting to another origin


def test_app_policy_still_allows_what_the_console_needs(client):
    """
    A policy that breaks the UI gets switched off, so it has to permit the real
    dependencies: the bundled assets, inline style attributes from React, and
    data: URIs for inline icons.
    """
    csp = _csp_directives(client.get("/health").headers["Content-Security-Policy"])
    assert "'unsafe-inline'" in csp["style-src"]
    assert "data:" in csp["img-src"]
    assert csp["connect-src"] == ["'self'"]


def test_app_policy_allows_no_third_party_origin(client):
    """
    The console loads no web fonts and no CDN assets, so nothing it renders should
    be able to reach another host — that includes styles and fonts, which is where
    the Google Fonts allowance used to sit.
    """
    csp = _csp_directives(client.get("/health").headers["Content-Security-Policy"])
    for directive in ("default-src", "script-src", "style-src", "font-src", "img-src", "connect-src"):
        remote = [t for t in csp.get(directive, []) if t.startswith("http")]
        assert remote == [], f"{directive} allows {remote}"


# --- docs get their own, looser policy -------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_policy_allows_the_swagger_cdn(client, path):
    csp = _csp_directives(client.get(path).headers["Content-Security-Policy"])
    assert "https://cdn.jsdelivr.net" in csp["script-src"]
    # FastAPI's inline bootstrap is allowed by nonce, not by 'unsafe-inline' —
    # see tests/test_docs_csp.py.
    assert any(token.startswith("'nonce-") for token in csp["script-src"])
    assert "'unsafe-inline'" not in csp["script-src"]
    assert csp["frame-ancestors"] == ["'none'"]    # still not embeddable


def test_the_loose_docs_policy_does_not_leak_onto_app_routes(client):
    """A CDN allowance on /docs must not become a CDN allowance for the console."""
    csp = _csp_directives(client.get("/health").headers["Content-Security-Policy"])
    assert "https://cdn.jsdelivr.net" not in csp["script-src"]


# --- HSTS ------------------------------------------------------------------------

def test_hsts_is_not_sent_over_plain_http(client):
    """Pinning localhost to HTTPS for a year would break local development."""
    assert "Strict-Transport-Security" not in client.get("/health").headers


def test_hsts_is_sent_over_https(client):
    r = client.get("https://testserver/health")
    assert "max-age=" in r.headers["Strict-Transport-Security"]


def test_hsts_honours_forwarded_proto_only_when_proxies_are_trusted(client, monkeypatch):
    """
    Behind a TLS-terminating proxy the scheme here is http. Trust the forwarded
    header under the same opt-in that governs forwarded client IPs — otherwise
    anyone could set it.
    """
    headers = {"x-forwarded-proto": "https"}
    monkeypatch.delenv("UNILLM_TRUST_PROXY_HEADERS", raising=False)
    assert "Strict-Transport-Security" not in client.get("/health", headers=headers).headers

    monkeypatch.setenv("UNILLM_TRUST_PROXY_HEADERS", "true")
    assert "Strict-Transport-Security" in client.get("/health", headers=headers).headers


def test_hsts_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("UNILLM_HSTS_MAX_AGE", "0")
    assert "Strict-Transport-Security" not in client.get("https://testserver/health").headers


def test_malformed_hsts_max_age_falls_back_to_the_default(client, monkeypatch):
    monkeypatch.setenv("UNILLM_HSTS_MAX_AGE", "not-a-number")
    assert "max-age=" in client.get("https://testserver/health").headers["Strict-Transport-Security"]


# --- escape hatches ---------------------------------------------------------------

def test_csp_can_be_replaced(client, monkeypatch):
    monkeypatch.setenv("UNILLM_CSP", "default-src 'self' https://cdn.example.com")
    assert client.get("/health").headers["Content-Security-Policy"] == "default-src 'self' https://cdn.example.com"


def test_csp_can_be_turned_off_without_losing_the_other_headers(client, monkeypatch):
    monkeypatch.setenv("UNILLM_CSP", "off")
    r = client.get("/health")
    assert "Content-Security-Policy" not in r.headers
    assert r.headers["X-Frame-Options"] == "DENY"


def test_all_headers_can_be_turned_off(client, monkeypatch):
    monkeypatch.setenv("UNILLM_SECURITY_HEADERS", "false")
    r = client.get("/health")
    for header in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert header not in r.headers


def test_docs_override_is_independent_of_the_app_csp_override(client, monkeypatch):
    """Replacing the app policy must not silently break the docs pages."""
    monkeypatch.setenv("UNILLM_CSP", "default-src 'none'")
    csp = _csp_directives(client.get("/docs").headers["Content-Security-Policy"])
    assert "https://cdn.jsdelivr.net" in csp["script-src"]
