"""The registry (control-plane) database: the list of units and the public,
cross-cutting facts about them. This is the one shared database; everything
private to a unit lives in that unit's own database instead.

Its location comes from ``REGISTRY_DATABASE_URL`` (default a local
``registry.db``). It is deliberately separate from the per-unit schema in
``db/models.py`` and manages itself with ``create_all`` — it is not part of
the unit Alembic history.
"""
import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

REGISTRY_DATABASE_URL = os.getenv("REGISTRY_DATABASE_URL", "sqlite:///registry.db")

RegistryBase = declarative_base()


def _utcnow() -> datetime:
    return datetime.utcnow()


class Tenant(RegistryBase):
    """One unit on the platform. Holds only public / routing information;
    the unit's roster and records live in its own database at ``db_url``."""

    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False, unique=True)   # subdomain label, e.g. "5thva"
    name = Column(String, nullable=False)
    motto = Column(String, nullable=True)
    blurb = Column(Text, nullable=True)                  # public directory description
    brand_color = Column(Integer, nullable=False, default=0x7C1F2B)

    recruiting_open = Column(Boolean, nullable=False, default=True)
    listed = Column(Boolean, nullable=False, default=True)  # show in the public directory

    discord_guild_id = Column(BigInteger, nullable=True, unique=True)
    db_url = Column(String, nullable=False)              # the unit's private database

    is_default = Column(Boolean, nullable=False, default=False)  # the apex / legacy unit
    created_at = Column(DateTime, default=_utcnow)

    # Directory categorisation, for search/filter on the public listing.
    game = Column(String, nullable=True)    # e.g. "War of Rights", "Squad"
    tags = Column(String, nullable=True)    # comma-separated free tags (playstyle, region)


class ProfilePrefs(RegistryBase):
    """A platform-wide member's cross-unit profile settings, keyed by their
    Discord ID (stable across every unit). Only holds visibility preferences;
    the service record itself is aggregated live from each unit's database."""

    __tablename__ = "profile_prefs"

    discord_id = Column(BigInteger, primary_key=True)
    # When true, the member's positive service record (units, ranks, awards) is
    # visible to anyone. Recruiters can always see the vetting view regardless.
    public = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


_connect_args = {"check_same_thread": False} if REGISTRY_DATABASE_URL.startswith("sqlite") else {}
registry_engine = create_engine(REGISTRY_DATABASE_URL, connect_args=_connect_args)
RegistrySession = sessionmaker(bind=registry_engine, expire_on_commit=False)


def init_registry():
    """Create the registry schema if it doesn't exist yet, then add any columns
    that were introduced after a deployment first created the table."""
    RegistryBase.metadata.create_all(registry_engine)
    _ensure_columns()


def _ensure_columns():
    """create_all never alters an existing table, so add newly-introduced
    columns idempotently — this stands in for Alembic on the self-managed
    registry, letting new fields land on existing deployments on startup."""
    from sqlalchemy import inspect, text

    insp = inspect(registry_engine)
    if "tenants" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("tenants")}
    wanted = {"game": "VARCHAR", "tags": "VARCHAR"}
    with registry_engine.begin() as conn:
        for col, coltype in wanted.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE tenants ADD COLUMN {col} {coltype}"))


@contextmanager
def registry_session():
    """A registry session as a context manager."""
    session = RegistrySession()
    try:
        yield session
    finally:
        session.close()


def profile_is_public(discord_id: int) -> bool:
    """Whether this member has opted their service record into public view."""
    with registry_session() as s:
        prefs = s.get(ProfilePrefs, discord_id)
        return bool(prefs and prefs.public)


def set_profile_public(discord_id: int, public: bool) -> None:
    with registry_session() as s:
        prefs = s.get(ProfilePrefs, discord_id)
        if prefs is None:
            prefs = ProfilePrefs(discord_id=discord_id, public=public)
            s.add(prefs)
        else:
            prefs.public = public
        s.commit()
