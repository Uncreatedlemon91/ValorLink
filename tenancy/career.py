"""The durable career log.

A member's service is normally read live from each unit's database. That works
until a unit is *deleted* — its database leaves the registry and its slice of a
member's history would vanish. To prevent that, ``snapshot_unit`` copies every
member's key lifecycle facts into the registry the moment before a unit is
removed, reading the unit's final state so it captures history made through both
the website and the Discord bot.

The log is therefore tiny and write-rarely: it only grows when a unit is
deleted, and is read only when assembling a service record.
"""
from __future__ import annotations

import re

from tenancy.registry import CareerEvent, registry_session

# Same fixed shape the web and bot both write promotion history in.
_PROMOTION_RE = re.compile(r"^Promoted from .+ to (.+?) by ")


def snapshot_unit(db_url: str, slug: str, name: str) -> int:
    """Copy every member's lifecycle facts from a unit about to be deleted into
    the durable log. Returns the number of events recorded."""
    from db.models import AwardType, Member, MemberAward
    from tenancy.units import sessionmaker_for

    events: list[tuple[int, str, str | None, object]] = []
    try:
        with sessionmaker_for(db_url)() as s:
            for m in s.query(Member).all():
                events.append((m.discord_id, "enlisted", None, m.joined_date))
                discharge_at = None
                for h in m.service_history:
                    pm = _PROMOTION_RE.match(h.entry or "")
                    if pm:
                        events.append((m.discord_id, "promoted", pm.group(1).rstrip("."), h.date))
                    elif "discharged" in (h.entry or "").lower():
                        discharge_at = h.date
                rows = (
                    s.query(MemberAward, AwardType)
                    .join(AwardType, MemberAward.award_type_id == AwardType.id)
                    .filter(MemberAward.member_id == m.discord_id)
                    .all()
                )
                for ma, at in rows:
                    events.append((m.discord_id, "awarded", at.name, ma.date_awarded))
                if m.status == "discharged":
                    events.append((m.discord_id, "discharged", m.discharge_type,
                                   discharge_at or m.rank_since))
    except Exception:  # noqa: BLE001 -- a broken unit DB shouldn't block deletion
        return 0

    if not events:
        return 0
    with registry_session() as rs:
        for discord_id, kind, detail, at in events:
            rs.add(CareerEvent(discord_id=discord_id, unit_slug=slug, unit_name=name,
                               kind=kind, detail=detail, at=at))
        rs.commit()
    return len(events)


def career_events_for(discord_id: int) -> list[dict]:
    """Every archived career event for a member, oldest first."""
    with registry_session() as s:
        rows = (
            s.query(CareerEvent)
            .filter(CareerEvent.discord_id == discord_id)
            .order_by(CareerEvent.at)
            .all()
        )
        return [
            {"unit_slug": r.unit_slug, "unit_name": r.unit_name,
             "kind": r.kind, "detail": r.detail, "at": r.at}
            for r in rows
        ]
