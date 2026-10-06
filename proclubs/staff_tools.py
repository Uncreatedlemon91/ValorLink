"""Staff tools: the action inbox and the squad planner.

The inbox is everything waiting on a decision, worked out from the data
rather than kept as a to-do list, so it can't go stale: a contract
running out, a match with no team sheet, a report not written, players
nobody can track. Each item links to where it's dealt with.

The planner is a what-if: change squad statuses, release players, add
signings you're considering, and see the depth chart and status balance
that would leave -- before anybody is offered anything. It saves
nothing; contracts change through offers and renewals as always.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

import discord_roster
import matchweek as mw
import recruitment
import roles
import squad
from models import Event, PlayerLink, RosterMove
from services import (CONTRACT_EXPIRED, CONTRACT_EXPIRING, contract_state, contract_time_left,
                      live_contracts, offer_awaits_confirmation)

PLANNER_STATUSES = roles.SQUAD_STATUSES + ("Release",)
MAX_PLANNED_SIGNINGS = 4


def _item(level: str, text: str, href: str, count: int | None = None) -> dict:
    return {"level": level, "text": text, "href": href, "count": count}


def action_items(session: Session, *, management: bool, now: datetime | None = None) -> list[dict]:
    """What needs a decision, most urgent first. "warn" items need doing
    soon; "info" ones are housekeeping."""
    now = now or datetime.utcnow()
    items: list[dict] = []
    contracts = live_contracts(session)

    if management:
        for c in contracts:
            state = contract_state(c, now)
            if state == CONTRACT_EXPIRED:
                items.append(_item("warn", f"{c.display_name}'s contract has run out "
                                           f"({contract_time_left(c, now)}). Renew or release.", "/roster"))
            elif state == CONTRACT_EXPIRING:
                items.append(_item("warn", f"{c.display_name}'s contract: {contract_time_left(c, now)}.",
                                   "/roster"))
        awaiting = [m for m in session.execute(select(RosterMove).where(
            RosterMove.kind.in_(discord_roster.CONFIRMABLE_KINDS))).scalars()
            if offer_awaits_confirmation(m)]
        if awaiting:
            names = ", ".join(m.display_name for m in awaiting[:3])
            items.append(_item("warn", f"Accepted, waiting for you to confirm: {names}"
                                       f"{' and more' if len(awaiting) > 3 else ''}.", "/roster", len(awaiting)))

    upcoming = session.execute(select(Event).where(
        Event.scheduled_at > now, Event.scheduled_at <= now + timedelta(hours=72))
        .order_by(Event.scheduled_at)).scalars()
    for e in upcoming:
        if mw.is_match(e) and not e.lineup_published_at:
            items.append(_item("warn", f"No team sheet yet for {e.title} "
                                       f"({e.scheduled_at.strftime('%a %-d %b')}).", f"/events/{e.id}/teamsheet"))
        if e.scheduled_at <= now + timedelta(hours=48):
            missing = len(mw.unanswered(session, e))
            if missing:
                items.append(_item("info", f"{missing} player{'s haven' if missing != 1 else ' hasn'}'t "
                                           f"answered {e.title}.", f"/events/{e.id}", missing))

    recent = session.execute(select(Event).where(
        Event.scheduled_at <= now - mw.VOTE_OPENS_AFTER, Event.scheduled_at > now - timedelta(days=7),
        Event.review_published_at.is_(None))).scalars()
    for e in recent:
        if mw.is_match(e):
            items.append(_item("info", f"No match report for {e.title} yet.", f"/events/{e.id}/report"))

    settling = recruitment.settling_in(session)
    if settling:
        names = ", ".join(p.name for p, _ in settling[:3])
        items.append(_item("info", f"Still settling in: {names}{' and more' if len(settling) > 3 else ''} "
                                   f"— finish their checklist.", "/recruitment", len(settling)))
    unrated = recruitment.trialists_without_notes(session)
    if unrated:
        items.append(_item("info", f"{len(unrated)} trialist{'s have' if len(unrated) != 1 else ' has'} "
                                   f"no feedback yet.", "/recruitment", len(unrated)))

    linked = {str(i) for i in session.execute(select(PlayerLink.discord_user_id)).scalars()}
    unlinked = [c for c in contracts if c.discord_id not in linked]
    if unlinked:
        items.append(_item("info", f"{len(unlinked)} player{'s have' if len(unlinked) != 1 else ' has'} "
                                   f"no gamertag linked, so their stats aren't tracked.", "/squad", len(unlinked)))
    unset = mw.players_without_pattern(session)
    if unset:
        items.append(_item("info", f"{len(unset)} player{'s haven' if len(unset) != 1 else ' hasn'}'t set "
                                   f"their usual nights.", "/availability", len(unset)))

    from models import TrainingSuggestion
    open_suggestions = session.execute(select(TrainingSuggestion).where(
        TrainingSuggestion.status == "open")).scalars().all()
    if open_suggestions:
        items.append(_item("info", f"{len(open_suggestions)} training suggestion"
                                   f"{'s' if len(open_suggestions) != 1 else ''} to answer.",
                           "/training#suggestions", len(open_suggestions)))

    from development import open_goal_counts
    with_goals = open_goal_counts(session)
    no_goal = [c for c in contracts if not with_goals.get(c.discord_id)]
    if contracts and no_goal:
        items.append(_item("info", f"{len(no_goal)} player{'s have' if len(no_goal) != 1 else ' has'} "
                                   f"no development goal.", "/players", len(no_goal)))

    order = {"warn": 0, "info": 1}
    return sorted(items, key=lambda i: order[i["level"]])


# --------------------------------------------------------------------------- #
# Squad planner
# --------------------------------------------------------------------------- #
def plan_squad(contracts: list, changes: dict[str, str], signings: list[dict]) -> list:
    """The squad as it would be: contracts with their planned status
    (`changes`: discord_id -> status, "Release" drops them), plus planned
    signings ({name, position, secondary, status}). Contract-like objects,
    so squad.squad_depth reads them as it reads real ones."""
    planned = []
    for c in contracts:
        status = changes.get(c.discord_id) or c.squad_status
        if status == "Release":
            continue
        planned.append(SimpleNamespace(display_name=c.display_name, position=c.position,
                                       secondary_position=c.secondary_position,
                                       squad_status=status, discord_id=c.discord_id, planned=False))
    for s in signings:
        if not s.get("position"):
            continue
        planned.append(SimpleNamespace(display_name=s.get("name") or "New signing",
                                       position=s["position"], secondary_position=s.get("secondary") or None,
                                       squad_status=s.get("status") or "Rotation",
                                       discord_id=None, planned=True))
    return planned


def plan_summary(planned: list, slots: dict) -> dict:
    """Depth for everyone, depth for the starters alone (can they field the
    XI?), and how many are at each status."""
    starters = [p for p in planned if p.squad_status == "Starter"]
    by_status = {s: sum(1 for p in planned if p.squad_status == s) for s in roles.SQUAD_STATUSES}
    return {
        "depth": squad.squad_depth(slots, planned),
        "starter_depth": squad.squad_depth(slots, starters),
        "by_status": by_status,
        "size": len(planned),
        "slots": len(slots),
    }


def parse_planner_form(form, contracts: list) -> tuple[dict[str, str], list[dict]]:
    """What the planner form asks for, keeping only valid values."""
    allowed = set(PLANNER_STATUSES)
    positions = set(discord_roster.PITCH_POSITIONS)
    changes = {}
    for c in contracts:
        value = form.get(f"status__{c.discord_id}")
        if value in allowed and value != c.squad_status:
            changes[c.discord_id] = value
    signings = []
    for i in range(MAX_PLANNED_SIGNINGS):
        position = form.get(f"new_position__{i}") or ""
        if position not in positions:
            continue
        secondary = form.get(f"new_secondary__{i}") or ""
        status = form.get(f"new_status__{i}") or "Rotation"
        signings.append({"name": (form.get(f"new_name__{i}") or "").strip()[:40] or f"Signing {i + 1}",
                         "position": position,
                         "secondary": secondary if secondary in positions and secondary != position else None,
                         "status": status if status in roles.SQUAD_STATUSES else "Rotation"})
    return changes, signings
