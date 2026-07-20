"""The cross-unit service record.

A member is the same person on every unit — each unit's database keys its
roster by the member's Discord ID — so a platform-wide "service record" is an
aggregation over every unit's database for one Discord ID: where they serve
now, where they've served, their promotions, awards, and how they left.

Every service record is public. Visibility only controls how much detail the
viewer sees (see ``viewer_level``):

* **owner**     — the member themselves: everything, including the written
                  reasons behind discharges and the full per-unit history.
* **recruiter** — anyone holding recruiter standing or higher on any unit:
                  the vetting view — service, ranks, tenure, awards, and the
                  *type* of any discharge (honorable / dishonorable), but never
                  the written reasons or disciplinary notes.
* **public**    — everyone else, including signed-out visitors: the positive
                  record — units, ranks, tenure, awards, and promotions. No
                  discharge type, no reasons.
"""
from __future__ import annotations

import os
import re

from db.models import AwardType, Member, MemberAward, ServiceHistoryEntry
from tenancy.career import career_events_for
from tenancy.resolve import all_tenants
from tenancy.units import sessionmaker_for

LEVEL_OWNER = "owner"
LEVEL_RECRUITER = "recruiter"
LEVEL_PUBLIC = "public"

# Promotion entries are written in a fixed shape by both the web and the bot:
# "Promoted from <old> to <new> by <actor>." — pull the new rank, drop the rest.
_PROMOTION_RE = re.compile(r"^Promoted from .+ to (.+?) by ")


def search_players(query: str, limit: int = 40) -> list[dict]:
    """Find members across every unit by callsign, aggregated by Discord ID.
    Search-driven (no full enumeration) — returns matches with the units each
    serves in, linkable to their service record."""
    from tenancy.registry import registry_session

    q = query.strip()
    if len(q) < 2:
        return []
    with registry_session() as rs:
        tenants = [(t.name, t.db_url) for t in all_tenants(rs)]

    found: dict[int, dict] = {}
    for unit_name, db_url in tenants:
        try:
            with sessionmaker_for(db_url)() as s:
                rows = (
                    s.query(Member)
                    .filter(Member.callsign.ilike(f"%{q}%"))
                    .limit(200)
                    .all()
                )
                for m in rows:
                    entry = found.get(m.discord_id)
                    if entry is None:
                        entry = {"discord_id": m.discord_id, "name": m.callsign,
                                 "avatar": m.avatar, "units": [], "active": False}
                        found[m.discord_id] = entry
                    entry["units"].append(unit_name)
                    if m.status == "active":
                        entry["active"] = True
                        entry["name"] = m.callsign
                        if m.avatar:
                            entry["avatar"] = m.avatar
        except Exception:  # noqa: BLE001 -- skip an unreadable unit
            continue

    results = sorted(found.values(), key=lambda r: (not r["active"], r["name"].lower()))
    return results[:limit]


def viewer_level(viewer: dict | None, target_id: int) -> str:
    """How much of ``target_id``'s record ``viewer`` may see. Every record is
    public, so this never denies access — it only picks the detail level."""
    from web import auth  # local import avoids a load-time cycle

    if viewer and str(viewer.get("id")) == str(target_id):
        return LEVEL_OWNER
    tiers = (viewer or {}).get("tiers") or {}
    recruiter_floor = auth._ORDER[auth.TIER_RECRUITER]
    if any(auth._ORDER.get(t, 0) >= recruiter_floor for t in tiers.values()):
        return LEVEL_RECRUITER
    return LEVEL_PUBLIC


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

    # Structured, reason-free promotions parsed from the service history — the
    # new rank and date only, safe to show at any level.
    promotions = []
    discharged_at = None
    for h in member.service_history:
        m = _PROMOTION_RE.match(h.entry or "")
        if m:
            promotions.append({"rank": m.group(1).rstrip("."), "date": h.date})
        elif "discharged" in (h.entry or "").lower():
            discharged_at = h.date  # the date only, never the written reason

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
        "promotions": promotions,
        "discharged_at": discharged_at if member.status == "discharged" else None,
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


def _archived_service(discord_id: int, live_slugs: set[str], level: str):
    """Reconstruct service in now-deleted units from the durable career log.
    Returns (postings, milestones) for units no longer live on the platform, so
    a member's history survives a unit's removal."""
    events = [e for e in career_events_for(discord_id) if e["unit_slug"] not in live_slugs]
    by_unit: dict[str, list] = {}
    for e in events:
        by_unit.setdefault(e["unit_slug"], []).append(e)

    postings, milestones = [], []
    for slug, evs in by_unit.items():
        unit = evs[0]["unit_name"]
        enlisted = next((e["at"] for e in evs if e["kind"] == "enlisted"), None)
        promotions = [e for e in evs if e["kind"] == "promoted"]
        awards = [{"name": e["detail"], "emoji": None, "date": e["at"]}
                  for e in evs if e["kind"] == "awarded"]
        discharge = next((e for e in evs if e["kind"] == "discharged"), None)
        final_rank = promotions[-1]["detail"] if promotions else None
        postings.append({
            "unit": unit, "slug": slug, "url": None, "archived": True,
            "rank": final_rank or "—", "company": "",
            "status": "discharged" if discharge else "departed",
            "joined": enlisted, "rank_since": None,
            "awards": awards, "promotions": [], "history": [],
            "discharge_type": discharge["detail"] if (discharge and level in (LEVEL_OWNER, LEVEL_RECRUITER)) else None,
        })
        # Timeline entries — reason-free by construction.
        if enlisted:
            milestones.append({"at": enlisted, "kind": "enlisted", "unit": unit,
                               "text": f"Enlisted in {unit}"})
        for pr in promotions:
            milestones.append({"at": pr["at"], "kind": "promote", "unit": unit,
                               "text": f"Promoted to {pr['detail']} · {unit}"})
        for a in awards:
            milestones.append({"at": a["date"], "kind": "award", "unit": unit,
                               "text": f"Awarded {a['name']} · {unit}"})
        if discharge:
            dt = discharge["detail"]
            label = f"{dt.title()} discharge" if dt else "Departed"
            milestones.append({"at": discharge["at"], "kind": "discharge", "unit": unit,
                               "text": f"{label} · {unit} (archived)"})
    return postings, milestones


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

    # Service in units that have since been deleted, from the durable log.
    live_slugs = {p["slug"] for p in postings}
    archived, archived_milestones = _archived_service(discord_id, live_slugs, level)
    if archived and name is None:
        name = f"ID {discord_id}"

    all_postings = postings + archived
    current = [p for p in postings if p["status"] != "discharged"]
    former = [p for p in postings if p["status"] == "discharged"] + archived

    joined_dates = [p["joined"] for p in all_postings if p["joined"]]
    awards_total = sum(len(p["awards"]) for p in all_postings)
    stats = {
        "units_served": len(all_postings),
        "active_units": len(current),
        "awards_total": awards_total,
        "first_enlisted": min(joined_dates) if joined_dates else None,
    }
    if level in (LEVEL_OWNER, LEVEL_RECRUITER):
        stats["dishonorable"] = sum(
            1 for p in former if p.get("discharge_type") == "dishonorable")

    # A reason-free milestone feed, merged across units: enlistments, awards,
    # and departures. Owners also see promotions via each unit's full history.
    milestones = []
    for p in postings:
        if p["joined"]:
            milestones.append({"at": p["joined"], "kind": "enlisted",
                               "unit": p["unit"], "text": f"Enlisted in {p['unit']}"})
        for pr in p["promotions"]:
            milestones.append({"at": pr["date"], "kind": "promote", "unit": p["unit"],
                               "text": f"Promoted to {pr['rank']} · {p['unit']}"})
        for a in p["awards"]:
            if a["date"]:
                milestones.append({"at": a["date"], "kind": "award", "unit": p["unit"],
                                   "text": f"Awarded {a['name']} · {p['unit']}"})
        if p["status"] == "discharged":
            dt = p.get("discharge_type")
            label = f"{dt.title()} discharge" if dt else "Departed"
            milestones.append({"at": p.get("discharged_at") or p["rank_since"],
                               "kind": "discharge", "unit": p["unit"],
                               "text": f"{label} · {p['unit']}"})
    milestones += archived_milestones
    milestones.sort(key=lambda m: (m["at"] is None, m["at"]))

    return {
        "discord_id": discord_id,
        "name": name,
        "avatar": avatar,
        "level": level,
        "postings": all_postings,
        "current": current,
        "former": former,
        "stats": stats,
        "milestones": milestones,
        "found": bool(all_postings),
    }
