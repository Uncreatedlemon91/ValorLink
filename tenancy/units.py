"""Per-unit database access.

Each unit has its own database (a SQLite file by default). This module caches
one engine + sessionmaker per database URL and provisions the schema for a
brand-new unit. The schema is the normal ValorLink schema from
``db/models.py`` — every unit database is identical in shape, isolated in
content.
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base as UnitBase

REPO_ROOT = Path(__file__).resolve().parent.parent
# Where per-unit SQLite files are stored (one file per unit).
UNIT_DB_DIR = Path(os.getenv("UNIT_DB_DIR", str(REPO_ROOT / "units")))

_engines: dict = {}
_sessionmakers: dict = {}


def engine_for(db_url: str):
    if db_url not in _engines:
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        _engines[db_url] = create_engine(db_url, connect_args=connect_args)
    return _engines[db_url]


def sessionmaker_for(db_url: str) -> sessionmaker:
    if db_url not in _sessionmakers:
        _sessionmakers[db_url] = sessionmaker(bind=engine_for(db_url), expire_on_commit=False)
    return _sessionmakers[db_url]


def dispose_engine(db_url: str) -> None:
    """Drop a unit's cached engine and connections — used when a unit is deleted
    so no stale connection lingers against an archived database file."""
    sm = _sessionmakers.pop(db_url, None)
    if sm is not None:
        sm.close_all()
    engine = _engines.pop(db_url, None)
    if engine is not None:
        engine.dispose()


def session_for(tenant) -> Session:
    """Open a session against a tenant's private database."""
    return sessionmaker_for(tenant.db_url)()


def unit_db_url_for_slug(slug: str) -> str:
    """The default SQLite URL for a unit's private database file."""
    UNIT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(UNIT_DB_DIR / (slug + '.db')).resolve()}"


def _alembic_head() -> str | None:
    """Read the current migration head without touching any database."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def provision_unit_db(db_url: str):
    """Build the schema for a new unit database and stamp it at the current
    Alembic head, so later migrations apply cleanly to it."""
    engine = engine_for(db_url)
    UnitBase.metadata.create_all(engine)

    head = _alembic_head()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        if head:
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})
