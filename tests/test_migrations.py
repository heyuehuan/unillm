"""
Migrations have to run against a database that already has rows.

The failure this pins: b322fabf031b adds `stream` and `total_tokens` to
request_logs as NOT NULL. Their defaults live on the ORM model, which only
applies to rows this application inserts — the database has nothing to put in
the column for rows that already exist. So the upgrade succeeded on a fresh
install and failed on every real deployment, which is the worst way round.

The test therefore migrates to the revision *before* that one, writes a log row,
and only then upgrades to head.
"""
import os

import pytest
from sqlalchemy import create_engine, text

# The revision immediately before the one under test.
BEFORE = "b5b1d61839d4"


def _alembic_config(url):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    """Run alembic against a throwaway file database (in-memory would not survive)."""
    from alembic import command

    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    # alembic/env.py reads DATABASE_URL itself and overrides the config value.
    monkeypatch.setenv("DATABASE_URL", url)

    def run(revision):
        command.upgrade(_alembic_config(url), revision)
        return create_engine(url)

    return run


def test_upgrading_a_populated_database_keeps_its_request_logs(upgrade):
    engine = upgrade(BEFORE)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs "
            "(model, prompt_tokens, completion_tokens, created_at) "
            "VALUES ('gpt-4', 10, 5, '2026-01-01 00:00:00')"
        ))

    engine = upgrade("head")

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT model, prompt_tokens, stream, total_tokens FROM request_logs"
        )).one()
    assert row.model == "gpt-4"
    assert row.prompt_tokens == 10
    # Backfilled by the column's server_default, not left NULL in a NOT NULL column.
    assert not row.stream
    assert row.total_tokens == 0


def test_migrating_from_empty_still_reaches_head(upgrade):
    engine = upgrade("head")
    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"users", "projects", "api_keys", "request_logs",
            "audit_logs", "model_pricing"} <= tables
