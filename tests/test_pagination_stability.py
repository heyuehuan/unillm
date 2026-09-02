"""
Paging through logs must not skip or repeat rows.

Requests written in the same clock tick have no inherent order, so ordering by
timestamp alone lets the database return the tied group differently on each query.
With OFFSET paging that shows up as a row appearing on two pages and another
appearing on none.

SQLite happens to scan in rowid order, so a behavioural test cannot show the bug
on the test database — it only appears under a planner that is free to reorder the
tied group. The ordering itself is therefore asserted directly, on the SQL, with
the page-coverage checks kept as a sanity net.
"""

import pytest

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import AuditLog, Base, RequestLog


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def rows():
    """Sixty request logs and sixty audit logs, all stamped the same instant."""
    from datetime import datetime
    instant = datetime(2026, 1, 1, 12, 0, 0)
    db = SessionLocal()
    try:
        for i in range(60):
            db.add(RequestLog(request_id=f"tie-{i:02d}", model="gemini-2.0-flash",
                              status_code=200, created_at=instant))
            db.add(AuditLog(action="tie_test", username=f"user{i:02d}",
                            severity="info", created_at=instant))
        db.commit()
    finally:
        db.close()
    return instant


def _page_through(query, page_size, total_expected):
    seen = []
    offset = 0
    while offset < total_expected:
        db = SessionLocal()
        try:
            page, total = query(db, page_size, offset)
        finally:
            db.close()
        seen.extend(page)
        offset += page_size
    return seen


@pytest.mark.parametrize("query,table", [
    (lambda db: crud.query_request_logs(db, limit=5, offset=0), "request_logs"),
    (lambda db: crud.query_audit_logs(db, limit=5, offset=0), "audit_logs"),
])
def test_the_ordering_ends_in_a_unique_column(rows, query, table, monkeypatch):
    """Whatever the planner does with the tied timestamps, id settles it."""
    statements = []

    from sqlalchemy import event
    from unillm.db.database import engine as db_engine

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", record)
    try:
        db = SessionLocal()
        try:
            query(db)
        finally:
            db.close()
    finally:
        event.remove(db_engine, "before_cursor_execute", record)

    selects = [s for s in statements if "ORDER BY" in s and table in s]
    assert selects, statements
    order_by = selects[-1].split("ORDER BY")[1]
    assert f"{table}.created_at DESC" in order_by
    assert f"{table}.id DESC" in order_by


def test_request_log_pages_cover_every_row_exactly_once(rows):
    def query(db, limit, offset):
        page, total = crud.query_request_logs(db, limit=limit, offset=offset)
        return [r.request_id for r in page], total

    seen = _page_through(query, 7, 60)
    assert len(seen) == 60
    assert len(set(seen)) == 60
    assert set(seen) == {f"tie-{i:02d}" for i in range(60)}


def test_audit_log_pages_cover_every_row_exactly_once(rows):
    def query(db, limit, offset):
        page, total = crud.query_audit_logs(db, action="tie_test", limit=limit, offset=offset)
        return [r.username for r in page], total

    seen = _page_through(query, 7, 60)
    assert len(seen) == 60
    assert len(set(seen)) == 60


def test_the_same_page_comes_back_the_same_way_twice(rows):
    db = SessionLocal()
    try:
        first, _ = crud.query_request_logs(db, limit=10, offset=20)
        second, _ = crud.query_request_logs(db, limit=10, offset=20)
    finally:
        db.close()
    assert [r.request_id for r in first] == [r.request_id for r in second]


def test_newest_still_comes_first(rows):
    from datetime import datetime
    db = SessionLocal()
    try:
        db.add(RequestLog(request_id="newest", model="gemini-2.0-flash", status_code=200,
                          created_at=datetime(2026, 6, 1, 12, 0, 0)))
        db.commit()
        page, _ = crud.query_request_logs(db, limit=1, offset=0)
    finally:
        db.close()
    assert page[0].request_id == "newest"
