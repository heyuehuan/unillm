"""
Trusting X-Forwarded-For correctly.

The header is a list that any client can seed: whatever the caller sends is kept
and each proxy appends to it. Reading the first entry therefore reads the caller's
own claim, which lets an attacker pick a fresh rate-limit bucket for every request
and write any address they like into the audit trail.

These tests pin the fix: count entries from the right, one position per trusted
proxy, and ignore the header entirely when it is shorter than the configured chain
or when forwarded headers are not trusted at all.
"""

import pytest
from fastapi.testclient import TestClient

from unillm.proxy import forwarded, ratelimit
from unillm.proxy.proxy_server import app
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


@pytest.fixture(autouse=True)
def _clean_warning_state():
    forwarded.reset_warning_state()
    yield


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Just the two attributes the resolver reads."""

    def __init__(self, headers, peer="10.0.0.9"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = _FakeClient(peer) if peer else None


# --- address resolution --------------------------------------------------------------

def test_forwarded_header_is_ignored_without_the_opt_in(monkeypatch):
    monkeypatch.delenv(forwarded.TRUST_ENV, raising=False)
    request = _FakeRequest({"x-forwarded-for": "1.2.3.4"})
    assert forwarded.client_ip(request) == "10.0.0.9"


def test_a_single_proxy_yields_the_address_the_proxy_saw(monkeypatch):
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.delenv(forwarded.HOPS_ENV, raising=False)
    request = _FakeRequest({"x-forwarded-for": "203.0.113.7"})
    assert forwarded.client_ip(request) == "203.0.113.7"


def test_a_spoofed_leading_entry_is_discarded(monkeypatch):
    """
    The attack: the caller sends its own X-Forwarded-For, nginx appends the address
    it actually saw. Reading the first entry returns the attacker's invention, so
    every request lands in a different rate-limit bucket and the audit trail records
    a fabricated address. Only the entry nginx wrote may be believed.
    """
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.delenv(forwarded.HOPS_ENV, raising=False)
    request = _FakeRequest({"x-forwarded-for": "9.9.9.9, 203.0.113.7"})
    assert forwarded.client_ip(request) == "203.0.113.7"


def test_two_proxies_count_two_entries_from_the_right(monkeypatch):
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.setenv(forwarded.HOPS_ENV, "2")
    request = _FakeRequest({"x-forwarded-for": "9.9.9.9, 203.0.113.7, 172.16.0.1"})
    assert forwarded.client_ip(request) == "203.0.113.7"


def test_a_header_shorter_than_the_chain_falls_back_to_the_peer(monkeypatch):
    """
    Two proxies are configured but only one entry arrived, so the request did not
    come through the expected path. Nothing in the header was necessarily written by
    our infrastructure, so believe none of it.
    """
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.setenv(forwarded.HOPS_ENV, "2")
    request = _FakeRequest({"x-forwarded-for": "9.9.9.9"})
    assert forwarded.client_ip(request) == "10.0.0.9"


def test_whitespace_and_empty_entries_are_tolerated(monkeypatch):
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.delenv(forwarded.HOPS_ENV, raising=False)
    request = _FakeRequest({"x-forwarded-for": " 9.9.9.9 ,, 203.0.113.7 "})
    assert forwarded.client_ip(request) == "203.0.113.7"


def test_a_malformed_hop_count_falls_back_to_one(monkeypatch):
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.setenv(forwarded.HOPS_ENV, "not-a-number")
    assert forwarded.trusted_proxy_hops() == 1
    monkeypatch.setenv(forwarded.HOPS_ENV, "0")
    assert forwarded.trusted_proxy_hops() == 1


# --- forwarded protocol --------------------------------------------------------------

def test_forwarded_proto_reads_the_trusted_position(monkeypatch):
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.delenv(forwarded.HOPS_ENV, raising=False)
    assert forwarded.forwarded_proto_is_https(_FakeRequest({"x-forwarded-proto": "https"}))
    # A spoofed leading claim must not win over what the proxy wrote.
    assert not forwarded.forwarded_proto_is_https(
        _FakeRequest({"x-forwarded-proto": "https, http"})
    )


def test_forwarded_proto_is_ignored_without_the_opt_in(monkeypatch):
    monkeypatch.delenv(forwarded.TRUST_ENV, raising=False)
    assert not forwarded.forwarded_proto_is_https(_FakeRequest({"x-forwarded-proto": "https"}))


# --- the reason it matters -----------------------------------------------------------

def test_rotating_forwarded_addresses_cannot_refresh_the_login_budget(client, monkeypatch):
    """
    End-to-end version of the bypass: with a per-IP budget of three, a caller that
    invents a new X-Forwarded-For each time must still be counted as one client.
    """
    monkeypatch.setenv(forwarded.TRUST_ENV, "true")
    monkeypatch.delenv(forwarded.HOPS_ENV, raising=False)
    monkeypatch.setattr(ratelimit, "login_ip_limiter", ratelimit.SlidingWindowLimiter(3, 60.0))

    statuses = []
    for i in range(5):
        response = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "wrong-password"},
            # testclient's peer is "testclient"; the proxy appends it as the last hop.
            headers={"x-forwarded-for": f"9.9.9.{i}, 203.0.113.7"},
        )
        statuses.append(response.status_code)

    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429]
