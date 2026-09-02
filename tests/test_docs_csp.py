"""
/docs shares an origin with the admin UI, so script that runs there can read the
session token the UI keeps in localStorage. The docs policy therefore allows the
one inline script FastAPI emits by nonce, never by 'unsafe-inline'.
"""

import re

import pytest
from fastapi.testclient import TestClient

from unillm.proxy.proxy_server import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _nonce_of(response):
    match = re.search(r"'nonce-([^']+)'", response.headers["content-security-policy"])
    assert match, response.headers["content-security-policy"]
    return match.group(1)


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_the_docs_pages_allow_their_script_by_nonce_not_unsafe_inline(client, path):
    response = client.get(path)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    script_src = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
    assert "'unsafe-inline'" not in script_src
    assert f"'nonce-{_nonce_of(response)}'" in script_src


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_every_inline_script_carries_the_nonce_the_header_names(client, path):
    """
    ReDoc's page happens to have no inline script and Swagger UI's has one; either
    way, nothing inline may run without the nonce.
    """
    response = client.get(path)
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", response.text)
    assert all(f'nonce="{_nonce_of(response)}"' in tag for tag in inline)
    if path == "/docs":
        assert inline, "Swagger UI is expected to bootstrap from an inline script"


def test_each_response_gets_a_fresh_nonce(client):
    first = _nonce_of(client.get("/docs"))
    second = _nonce_of(client.get("/docs"))
    assert first != second
