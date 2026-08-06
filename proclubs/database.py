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

from sqlalchemy import create_engine
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


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
