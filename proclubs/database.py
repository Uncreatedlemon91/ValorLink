"""Database setup for the site's own content (articles, events, streamers).

A separate SQLite file from db.py's EA-stats history store (data/site.db vs
data/history.db) -- different shape, different lifecycle, no reason to share
a schema. Self-managed via create_all rather than Alembic: this is a small,
single-tenant app with no need for migration tooling.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# SITE_DB_PATH lets tests (and any deployment that wants the DB elsewhere)
# point this at a scratch location without touching code.
DB_PATH = Path(os.getenv("SITE_DB_PATH") or Path(__file__).parent / "data" / "site.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def init_db():
    import models  # noqa: F401  -- registers models on Base

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """Additive-only schema sync: adds any column a model declares that an
    already-existing table is missing (e.g. after a code update adds a
    field), so a redeploy doesn't need the DB file wiped. Still no real
    migration tool -- this never drops, renames, or alters a column, only
    adds new ones, which is all a create_all-managed app like this needs."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                coltype = column.type.compile(engine.dialect)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {coltype}'
                if column.server_default is not None:
                    ddl += f" DEFAULT '{column.server_default.arg}'"
                conn.execute(text(ddl))


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
