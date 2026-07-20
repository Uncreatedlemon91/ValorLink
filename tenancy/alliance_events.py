"""Joint events — events an alliance hosts across its member units.

An alliance event is cross-unit (any allied member can answer the call), so it
and its RSVPs live in the registry rather than any one unit's database.
"""
from __future__ import annotations

from datetime import datetime

from tenancy.alliances import AllianceError, _membership
from tenancy.registry import AllianceEvent, AllianceRSVP, registry_session

RSVP_STATUSES = ("accepted", "tentative", "declined")


def create_event(alliance_id: int, host_slug: str, name: str, event_type: str,
                 scheduled_at: datetime, description: str, created_by: int | None) -> str:
    """Schedule a joint event. The host unit must be an active alliance member."""
    name = (name or "").strip()
    if not name:
        raise AllianceError("The event needs a name.")
    if scheduled_at is None:
        raise AllianceError("Pick a date and time for the event.")
    with registry_session() as s:
        member = _membership(s, alliance_id, host_slug)
        if member is None or member.status != "active":
            raise AllianceError("Only a member unit can host a joint event.")
        s.add(AllianceEvent(
            alliance_id=alliance_id, host_slug=host_slug, name=name,
            event_type=(event_type or "Line Battle").strip() or "Line Battle",
            scheduled_at=scheduled_at, description=(description or "").strip() or None,
            created_by=created_by))
        s.commit()
    return f"Joint event “{name}” scheduled."


def _counts(session, event_id: int) -> dict:
    rows = (
        session.query(AllianceRSVP.status)
        .filter(AllianceRSVP.alliance_event_id == event_id)
        .all()
    )
    out = {s: 0 for s in RSVP_STATUSES}
    for (status,) in rows:
        if status in out:
            out[status] += 1
    return out


def upcoming_events(alliance_id: int, viewer_id: int | None = None, limit: int = 25) -> list[dict]:
    """Future joint events for an alliance, each with RSVP counts and, if a
    viewer is given, that viewer's own answer."""
    now = datetime.utcnow()
    with registry_session() as s:
        events = (
            s.query(AllianceEvent)
            .filter(AllianceEvent.alliance_id == alliance_id,
                    AllianceEvent.scheduled_at > now)
            .order_by(AllianceEvent.scheduled_at)
            .limit(limit)
            .all()
        )
        out = []
        for e in events:
            mine = None
            if viewer_id is not None:
                r = (
                    s.query(AllianceRSVP)
                    .filter(AllianceRSVP.alliance_event_id == e.id,
                            AllianceRSVP.discord_id == viewer_id)
                    .one_or_none()
                )
                mine = r.status if r else None
            out.append({
                "id": e.id, "name": e.name, "event_type": e.event_type,
                "scheduled_at": e.scheduled_at, "description": e.description,
                "host_slug": e.host_slug, "counts": _counts(s, e.id), "mine": mine,
            })
        return out


def next_event_for_alliances(alliance_ids: list[int]) -> dict | None:
    """The single soonest upcoming joint event across the given alliances."""
    if not alliance_ids:
        return None
    now = datetime.utcnow()
    with registry_session() as s:
        e = (
            s.query(AllianceEvent)
            .filter(AllianceEvent.alliance_id.in_(alliance_ids),
                    AllianceEvent.scheduled_at > now)
            .order_by(AllianceEvent.scheduled_at)
            .first()
        )
        if e is None:
            return None
        return {"id": e.id, "name": e.name, "event_type": e.event_type,
                "scheduled_at": e.scheduled_at, "alliance_id": e.alliance_id}


def rsvp(event_id: int, discord_id: int, unit_slug: str | None, status: str) -> str:
    if status not in RSVP_STATUSES:
        raise AllianceError("Choose accept, tentative, or decline.")
    with registry_session() as s:
        event = s.get(AllianceEvent, event_id)
        if event is None:
            raise AllianceError("That event no longer exists.")
        existing = (
            s.query(AllianceRSVP)
            .filter(AllianceRSVP.alliance_event_id == event_id,
                    AllianceRSVP.discord_id == discord_id)
            .one_or_none()
        )
        if existing is None:
            s.add(AllianceRSVP(alliance_event_id=event_id, discord_id=discord_id,
                               unit_slug=unit_slug, status=status))
        else:
            existing.status = status
            existing.unit_slug = unit_slug
            existing.responded_at = datetime.utcnow()
        s.commit()
    return "Answer recorded."


def event_alliance_id(event_id: int) -> int | None:
    with registry_session() as s:
        e = s.get(AllianceEvent, event_id)
        return e.alliance_id if e else None


# --- Bot-facing helpers (announce + remind across member guilds) ---------- #
def member_targets(alliance_id: int) -> list[dict]:
    """Active member units of an alliance with their Discord routing info."""
    from tenancy.registry import AllianceMember, Tenant

    with registry_session() as s:
        slugs = [
            r.unit_slug for r in s.query(AllianceMember).filter(
                AllianceMember.alliance_id == alliance_id,
                AllianceMember.status == "active").all()
        ]
        if not slugs:
            return []
        tenants = s.query(Tenant).filter(Tenant.slug.in_(slugs)).all()
        return [{"slug": t.slug, "guild_id": t.discord_guild_id, "db_url": t.db_url}
                for t in tenants]


def events_needing_announcement() -> list[dict]:
    """Future joint events not yet posted to member Discords."""
    now = datetime.utcnow()
    with registry_session() as s:
        rows = (
            s.query(AllianceEvent)
            .filter(AllianceEvent.announced.is_(False),
                    AllianceEvent.scheduled_at > now)
            .all()
        )
        return [{"id": e.id, "alliance_id": e.alliance_id, "name": e.name,
                 "event_type": e.event_type, "scheduled_at": e.scheduled_at,
                 "description": e.description, "host_slug": e.host_slug}
                for e in rows]


def events_needing_reminder(lead_minutes: int) -> list[dict]:
    """Joint events inside the reminder window that haven't been reminded yet,
    each with the members who answered accepted/tentative."""
    from datetime import timedelta
    now = datetime.utcnow()
    cutoff = now + timedelta(minutes=lead_minutes)
    with registry_session() as s:
        rows = (
            s.query(AllianceEvent)
            .filter(AllianceEvent.reminded.is_(False),
                    AllianceEvent.scheduled_at > now,
                    AllianceEvent.scheduled_at <= cutoff)
            .all()
        )
        out = []
        for e in rows:
            recipients = [
                {"discord_id": r.discord_id, "unit_slug": r.unit_slug}
                for r in s.query(AllianceRSVP).filter(
                    AllianceRSVP.alliance_event_id == e.id,
                    AllianceRSVP.status.in_(("accepted", "tentative"))).all()
            ]
            out.append({"id": e.id, "alliance_id": e.alliance_id, "name": e.name,
                        "event_type": e.event_type, "scheduled_at": e.scheduled_at,
                        "recipients": recipients})
        return out


def _mark(event_id: int, field: str) -> None:
    with registry_session() as s:
        e = s.get(AllianceEvent, event_id)
        if e is not None:
            setattr(e, field, True)
            s.commit()


def mark_announced(event_id: int) -> None:
    _mark(event_id, "announced")


def mark_reminded(event_id: int) -> None:
    _mark(event_id, "reminded")
