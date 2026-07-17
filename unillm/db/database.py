import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./unillm.db")

if DATABASE_URL == "sqlite:///:memory:":
    # StaticPool ensures all connections share the same in-memory database
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite"):
    # SQLite ships with foreign key enforcement OFF; without this, inserts
    # referencing nonexistent rows (e.g. an API key for a deleted project)
    # succeed silently.
    @event.listens_for(engine, "connect")
    def _sqlite_enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Run all pending Alembic migrations. Falls back to create_all for in-memory test DBs."""
    if DATABASE_URL == "sqlite:///:memory:":
        from unillm.db import models  # noqa: F401
        Base.metadata.create_all(bind=engine)
        return

    from alembic.config import Config
    from alembic import command
    import os

    alembic_cfg = Config()
    alembic_cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "..", "alembic"),
    )
    alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(alembic_cfg, "head")
