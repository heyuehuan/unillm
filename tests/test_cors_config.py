"""
A wildcard CORS allowlist must not come with credentials.

Starlette honours allow_origins=["*"] with allow_credentials=True by echoing the
requesting Origin back, so every site on the internet would be able to make
authenticated requests on behalf of a signed-in user.
"""

import pytest

from unillm.proxy.proxy_server import app, cors_settings


@pytest.mark.parametrize("raw,origins,credentials", [
    ("", [], False),
    ("https://app.example.com", ["https://app.example.com"], True),
    (" https://a.example.com , https://b.example.com ",
     ["https://a.example.com", "https://b.example.com"], True),
    ("*", ["*"], False),
    ("https://app.example.com,*", ["https://app.example.com", "*"], False),
])
def test_credentials_are_dropped_whenever_the_allowlist_is_a_wildcard(raw, origins, credentials):
    assert cors_settings(raw) == (origins, credentials)


def test_the_default_deployment_allows_no_cross_origin_use_at_all():
    origins, credentials = cors_settings("")
    assert origins == [] and credentials is False


def test_the_running_app_never_pairs_a_wildcard_with_credentials():
    for middleware in app.user_middleware:
        options = getattr(middleware, "kwargs", {})
        if "allow_origins" not in options:
            continue
        if "*" in options["allow_origins"]:
            assert options["allow_credentials"] is False
