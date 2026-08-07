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
    _drop_legacy_columns()


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


# Columns a past model used to declare, since removed, that an
# already-deployed database may still be carrying. Additive-only sync
# above only ever adds columns, so these linger after the code that used
# them is gone -- harmless for a nullable leftover, but NOT NULL ones
# (like this) break every future insert into that table, since a new row
# just never supplies a value for a column its model no longer knows
# about. SQLite has supported DROP COLUMN since 3.35 (2021), so this is
# safe to run unconditionally on every startup -- a no-op once it's gone.
_LEGACY_COLUMNS = {
    # Replaced by Article.body_html when the article editor became
    # Quill-based rich text instead of Markdown -- see html_sanitize.py.
    "articles": ["body_md"],
}


def _drop_legacy_columns():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, columns in _LEGACY_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
            for column_name in columns:
                if column_name in existing_columns:
                    conn.execute(text(f'ALTER TABLE {table_name} DROP COLUMN "{column_name}"'))


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
