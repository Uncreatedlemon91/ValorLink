"""The cross-unit service record.

A member is the same person on every unit — each unit's database keys its
roster by the member's Discord ID — so a platform-wide "service record" is an
aggregation over every unit's database for one Discord ID: where they serve
now, where they've served, their promotions, awards, and how they left.

Visibility is a hybrid (see ``viewer_level``):

* **owner**     — the member themselves: everything, including the written
                  reasons behind discharges and the full per-unit history.
* **recruiter** — anyone holding recruiter standing or higher on any unit:
                  the vetting view — service, ranks, tenure, awards, and the
                  *type* of any discharge (honorable / dishonorable), but never
                  the written reasons or disciplinary notes.
* **public**    — anyone, but only for members who opted their record public:
                  the positive record — units, ranks, tenure, awards. No
                  discharge type, no reasons.
"""
from __future__ import annotations

import os

from db.models import AwardType, Member, MemberAward, ServiceHistoryEntry
from tenancy.registry import profile_is_public
from tenancy.resolve import all_tenants
from tenancy.units import sessionmaker_for

LEVEL_OWNER = "owner"
LEVEL_RECRUITER = "recruiter"
LEVEL_PUBLIC = "public"


def viewer_level(viewer: dict | None, target_id: int) -> str | None:
    """How much of ``target_id``'s record ``viewer`` may see, or None if it's
    not visible to them at all."""
    from web import auth  # local import avoids a load-time cycle

    if viewer and str(viewer.get("id")) == str(target_id):
        return LEVEL_OWNER
    tiers = (viewer or {}).get("tiers") or {}
    recruiter_floor = auth._ORDER[auth.TIER_RECRUITER]
    if any(auth._ORDER.get(t, 0) >= recruiter_floor for t in tiers.values()):
        return LEVEL_RECRUITER
    if profile_is_public(target_id):
        return LEVEL_PUBLIC
    return None


def _unit_url(slug: str, is_default: bool) -> str:
    base = os.getenv("PLATFORM_BASE_DOMAIN")
    if not base or is_default:
        return "/"
    return f"https://{slug}.{base}/"


def _posting(session, member: Member, tenant, level: str) -> dict:
    """Build one unit's slice of the record, redacted to the viewer's level."""
    award_rows = (
        session.query(MemberAward, AwardType)
        .join(AwardType, MemberAward.award_type_id == AwardType.id)
        .filter(MemberAward.member_id == member.discord_id)
        .order_by(MemberAward.date_awarded)
        .all()
    )
    awards = [{"name": at.name, "emoji": at.emoji, "date": ma.date_awarded}
              for ma, at in award_rows]

    posting = {
        "unit": tenant.name,
        "slug": tenant.slug,
        "url": _unit_url(tenant.slug, tenant.is_default),
        "rank": member.rank,
        "company": member.company,
        "status": member.status,
        "joined": member.joined_date,
        "rank_since": member.rank_since,
        "awards": awards,
        "discharge_type": None,
        "history": [],
    }
    # The discharge *type* is a recruiter/owner signal; public sees only that
    # they're a former member, never why or how.
    if level in (LEVEL_OWNER, LEVEL_RECRUITER):
        posting["discharge_type"] = member.discharge_type
    # Only the member sees the raw written history (which carries reasons and
    # the names of officers who acted).
    if level == LEVEL_OWNER:
        posting["history"] = [
            {"date": h.date, "entry": h.entry}
            for h in sorted(member.service_history, key=lambda e: e.date)
        ]
    return posting


def build_service_record(discord_id: int, level: str) -> dict:
    """Aggregate ``discord_id``'s record across every unit, redacted to ``level``."""
    from tenancy.registry import registry_session

    with registry_session() as rs:
        tenants = sorted(all_tenants(rs), key=lambda t: t.name.lower())

    postings, name, avatar = [], None, None
    for tenant in tenants:
        try:
            with sessionmaker_for(tenant.db_url)() as s:
                member = s.get(Member, discord_id)
                if member is None:
                    continue
                postings.append(_posting(s, member, tenant, level))
                # Prefer the callsign/avatar from a unit they're still active in.
                if name is None or member.status == "active":
                    name = member.callsign
                    avatar = member.avatar
        except Exception:  # noqa: BLE001 -- an unreadable unit shouldn't sink the record
            continue

    current = [p for p in postings if p["status"] != "discharged"]
    former = [p for p in postings if p["status"] == "discharged"]

    joined_dates = [p["joined"] for p in postings if p["joined"]]
    awards_total = sum(len(p["awards"]) for p in postings)
    stats = {
        "units_served": len(postings),
        "active_units": len(current),
        "awards_total": awards_total,
        "first_enlisted": min(joined_dates) if joined_dates else None,
    }
    if level in (LEVEL_OWNER, LEVEL_RECRUITER):
        stats["dishonorable"] = sum(
            1 for p in former if p["discharge_type"] == "dishonorable")

    # A reason-free milestone feed, merged across units: enlistments, awards,
    # and departures. Owners also see promotions via each unit's full history.
    milestones = []
    for p in postings:
        if p["joined"]:
            milestones.append({"at": p["joined"], "kind": "enlisted",
                               "unit": p["unit"], "text": f"Enlisted in {p['unit']}"})
        for a in p["awards"]:
            if a["date"]:
                milestones.append({"at": a["date"], "kind": "award", "unit": p["unit"],
                                   "text": f"Awarded {a['name']} · {p['unit']}"})
        if p["status"] == "discharged":
            dt = p.get("discharge_type")
            label = f"{dt.title()} discharge" if dt else "Departed"
            milestones.append({"at": p["rank_since"], "kind": "discharge", "unit": p["unit"],
                               "text": f"{label} · {p['unit']}"})
    milestones.sort(key=lambda m: (m["at"] is None, m["at"]))

    return {
        "discord_id": discord_id,
        "name": name,
        "avatar": avatar,
        "level": level,
        "postings": postings,
        "current": current,
        "former": former,
        "stats": stats,
        "milestones": milestones,
        "found": bool(postings),
    }
