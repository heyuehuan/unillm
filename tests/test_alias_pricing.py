"""
An alias inherits its backend model's price.

Prices are stored per model name and were looked up by exact match on the name the
caller asked for. Aliases are the normal way to expose a variant of a model — a
CMEK deployment, a region, a per-team name — and each one needed its own price row
before it cost anything. Without one the request logged tokens and a NULL cost,
which the console shows as "—" and `/api/logs/stats` sums as zero, so real spend
was under-reported with nothing on screen saying part of the traffic was unpriced.

An alias is now priced by its own row if it has one, and otherwise by the row for
the backend model behind it. Inheriting is right whenever the variant bills the
same per token, which is the common case; when it does not, the alias gets its own
row and that wins. Because a wrong inheritance is invisible, the models list says
of every model whether its price is its own, inherited, or missing.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base, ModelPricing, RequestLog
from unillm.proxy.api_routes import hash_password
from unillm.proxy.proxy_server import app
from unillm.types import ChatCompletionResponse, Choice, Message, Usage

BASE = "gemini-2.5-flash-lite"
ALIAS = "gemini-2.5-flash-lite-kms"


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_auth(client):
    db = SessionLocal()
    try:
        crud.create_user(db=db, username="priceadmin",
                         hashed_password=hash_password("adminpass"), global_role="admin")
    finally:
        db.close()
    token = client.post("/api/auth/login",
                        json={"username": "priceadmin", "password": "adminpass"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def project_id(client, admin_auth):
    return client.post("/api/projects", json={"name": "pricing"}, headers=admin_auth).json()["id"]


@pytest.fixture(scope="module")
def api_key(client, admin_auth, project_id):
    return client.post(f"/api/projects/{project_id}/keys",
                       json={"name": "price-key", "allowed_models": ["all"]},
                       headers=admin_auth).json()["api_key"]


@pytest.fixture(autouse=True)
def clean(db_setup):
    """Prices and logs are what these tests assert on, so each starts from empty."""
    db = SessionLocal()
    try:
        db.query(RequestLog).delete()
        db.query(ModelPricing).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def price(db, model_name, input_per_1m, output_per_1m, notes=None):
    return crud.upsert_model_pricing(db, model_name=model_name, input_per_1m=input_per_1m,
                                     output_per_1m=output_per_1m, notes=notes)


@pytest.fixture
def backend(monkeypatch):
    """The alias is configured, and resolves to the base model behind it."""
    import unillm.proxy.proxy_server as ps

    handler = MagicMock()
    handler.SUPPORTED_CHAT_PARAMS = frozenset()
    handler.chat_completion = AsyncMock(return_value=ChatCompletionResponse(
        id="x", model=BASE, usage=Usage(prompt_tokens=1000, completion_tokens=2000),
        choices=[Choice(index=0, message=Message(role="assistant", content="hi"),
                        finish_reason="stop")],
    ))
    monkeypatch.setattr(ps, "_get_handler_for_model", lambda m: handler)
    monkeypatch.setattr(ps.proxy_config, "model_list", [
        {"model_name": ALIAS, "unillm_params": {"model": BASE, "model_type": "vertex-ai-kms"}},
    ])
    monkeypatch.setattr(ps, "general_settings", {})
    return handler


# ---------------------------------------------------------------------------
# Resolving the price
# ---------------------------------------------------------------------------

def test_a_model_with_its_own_price_uses_it(db):
    price(db, BASE, 0.10, 0.40)
    pricing, source = crud.resolve_model_pricing(db, BASE, backend_model=BASE)
    assert (pricing.input_per_1m, source) == (0.10, "exact")


def test_an_alias_without_a_price_inherits_the_backends(db):
    price(db, BASE, 0.10, 0.40)
    pricing, source = crud.resolve_model_pricing(db, ALIAS, backend_model=BASE)
    assert (pricing.model_name, source) == (BASE, "inherited")


def test_an_alias_with_its_own_price_overrides_the_backend(db):
    """The case inheritance must not swallow: a variant that really costs more."""
    price(db, BASE, 0.10, 0.40)
    price(db, ALIAS, 0.25, 1.00)
    pricing, source = crud.resolve_model_pricing(db, ALIAS, backend_model=BASE)
    assert (pricing.input_per_1m, pricing.output_per_1m, source) == (0.25, 1.00, "exact")


def test_an_unpriced_model_stays_unpriced(db):
    pricing, source = crud.resolve_model_pricing(db, ALIAS, backend_model=BASE)
    assert (pricing, source) == (None, "none")


def test_no_backend_means_no_inheritance(db):
    price(db, BASE, 0.10, 0.40)
    pricing, source = crud.resolve_model_pricing(db, ALIAS, backend_model=None)
    assert (pricing, source) == (None, "none")


def test_the_cost_is_the_backends_rate(db):
    price(db, BASE, 0.10, 0.40)
    cost = crud.compute_cost(db, ALIAS, prompt_tokens=1_000_000,
                             completion_tokens=1_000_000, backend_model=BASE)
    assert cost == pytest.approx(0.50)


def test_an_unpriced_model_costs_nothing_rather_than_zero(db):
    """None reads as "not known"; 0.0 would read as "free" and sum silently."""
    assert crud.compute_cost(db, ALIAS, 1_000_000, 1_000_000, backend_model=BASE) is None


# ---------------------------------------------------------------------------
# Through a real request
# ---------------------------------------------------------------------------

def _chat(client, key, model=ALIAS):
    return client.post("/v1/chat/completions",
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                       headers={"Authorization": f"Bearer {key}"})


def test_a_request_to_an_alias_is_costed(client, api_key, backend, db):
    price(db, BASE, 0.10, 0.40)
    assert _chat(client, api_key).status_code == 200

    row = db.query(RequestLog).order_by(RequestLog.id.desc()).first()
    assert (row.model, row.backend_model) == (ALIAS, BASE)
    # 1000 in at $0.10/1M plus 2000 out at $0.40/1M.
    assert row.cost_usd == pytest.approx(0.0009)


def test_the_alias_cost_reaches_the_usage_stats(client, api_key, admin_auth, backend, db):
    price(db, BASE, 0.10, 0.40)
    _chat(client, api_key)
    stats = client.get("/api/logs/stats", headers=admin_auth).json()
    assert stats["total_cost_usd"] == pytest.approx(0.0009)


def test_an_alias_priced_higher_than_its_backend_is_charged_at_its_own_rate(
        client, api_key, backend, db):
    price(db, BASE, 0.10, 0.40)
    price(db, ALIAS, 0.20, 0.80)
    _chat(client, api_key)
    row = db.query(RequestLog).order_by(RequestLog.id.desc()).first()
    assert row.cost_usd == pytest.approx(0.0018)


# ---------------------------------------------------------------------------
# Saying where the price came from
# ---------------------------------------------------------------------------

CONFIG = [{"model_name": ALIAS, "unillm_params": {"model": BASE, "model_type": "vertex-ai-kms"}}]


def summary_for(db, name, configured=CONFIG):
    return next(m for m in crud.get_models_summary(db, configured_models=configured)
                if m["name"] == name)


def test_an_inherited_price_is_labelled_and_shows_the_backends_rate(db):
    price(db, BASE, 0.10, 0.40)
    entry = summary_for(db, ALIAS)
    assert entry["pricing_source"] == "inherited"
    assert entry["pricing_from"] == BASE
    assert entry["pricing"]["input_per_1m"] == 0.10


def test_a_models_own_price_is_labelled_exact(db):
    price(db, ALIAS, 0.25, 1.00)
    entry = summary_for(db, ALIAS)
    assert entry["pricing_source"] == "exact"
    assert entry["pricing_from"] is None


def test_traffic_with_no_price_anywhere_is_labelled(db):
    crud.create_request_log(db=db, model=ALIAS, backend_model=BASE, status_code=200,
                            prompt_tokens=1, completion_tokens=1)
    entry = summary_for(db, ALIAS)
    assert entry["pricing_source"] == "none"
    assert entry["pricing"] is None
    assert entry["total_requests"] == 1


def test_an_inherited_note_does_not_describe_the_alias(db):
    """The backend's note is about the backend; showing it as the alias's would mislead."""
    price(db, BASE, 0.10, 0.40, notes="Standard flash-lite tier")
    entry = summary_for(db, ALIAS)
    assert entry["description"] is None
    assert entry["pricing"]["notes"] == "Standard flash-lite tier"


def test_a_model_dropped_from_the_config_still_inherits(db):
    """Its requests remember the backend even when the config no longer names it."""
    price(db, BASE, 0.10, 0.40)
    crud.create_request_log(db=db, model=ALIAS, backend_model=BASE, status_code=200,
                            prompt_tokens=1, completion_tokens=1)
    entry = summary_for(db, ALIAS, configured=[])
    assert entry["backend_model"] == BASE
    assert entry["pricing_source"] == "inherited"
