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


class Alliance(RegistryBase):
    """A coalition of units that have agreed to partner — a cross-unit group,
    so it lives here in the control-plane alongside the units themselves."""

    __tablename__ = "alliances"

    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False, unique=True)   # URL + short tag
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class CareerEvent(RegistryBase):
    """A durable record of a lifecycle event (enlisted, promoted, awarded,
    discharged) captured when a unit is removed from the platform, so a
    member's service in that unit survives the unit's deletion. Live units are
    read directly; this log only preserves what would otherwise be lost."""

    __tablename__ = "career_events"

    id = Column(Integer, primary_key=True)
    discord_id = Column(BigInteger, nullable=False, index=True)
    unit_slug = Column(String, nullable=False)
    unit_name = Column(String, nullable=False)
    kind = Column(String, nullable=False)    # enlisted | promoted | awarded | discharged
    detail = Column(String, nullable=True)   # rank name, award name, or discharge type
    at = Column(DateTime, nullable=True)


class AllianceMember(RegistryBase):
    """A unit's membership in an alliance. A membership is created as
    ``invited`` and only becomes ``active`` once an admin of that unit accepts —
    the mutual-consent handshake that stops a unit being claimed without say."""

    __tablename__ = "alliance_members"

    id = Column(Integer, primary_key=True)
    alliance_id = Column(Integer, nullable=False)
    unit_slug = Column(String, nullable=False)
    role = Column(String, nullable=False, default="member")     # founder | member
    status = Column(String, nullable=False, default="invited")  # invited | active
    invited_by_slug = Column(String, nullable=True)
    joined_at = Column(DateTime, default=_utcnow)


class AllianceEvent(RegistryBase):
    """A joint event hosted by an alliance (e.g. a coalition line battle).
    Cross-unit by nature, so it lives in the control-plane; members of any
    member unit can answer the call."""

    __tablename__ = "alliance_events"

    id = Column(Integer, primary_key=True)
    alliance_id = Column(Integer, nullable=False, index=True)
    host_slug = Column(String, nullable=False)     # the unit that scheduled it
    name = Column(String, nullable=False)
    event_type = Column(String, nullable=False, default="Line Battle")
    scheduled_at = Column(DateTime, nullable=False)
    description = Column(Text, nullable=True)
    created_by = Column(BigInteger, nullable=True)  # Discord id of the scheduler
    created_at = Column(DateTime, default=_utcnow)


class AllianceRSVP(RegistryBase):
    """A member's answer to a joint event — keyed by Discord id and the unit
    they answered from, since attendees span every allied unit."""

    __tablename__ = "alliance_rsvps"

    id = Column(Integer, primary_key=True)
    alliance_event_id = Column(Integer, nullable=False, index=True)
    discord_id = Column(BigInteger, nullable=False)
    unit_slug = Column(String, nullable=True)
    status = Column(String, nullable=False, default="accepted")  # accepted|tentative|declined
    responded_at = Column(DateTime, default=_utcnow)


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
