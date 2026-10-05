"""Training sessions: in-game practice and theory, planned on the site and
talked through in the fixture's Discord thread.

A session is an Event of type Training or Theory, so it has the same
sign-ups, formation, attendance and Discord announcement as a match. On
top it carries a plan:

  objective    one line -- "press when their CB turns back"
  plan         what the session does, in order
  role briefs  what each position does in it (slot key -> text), so a
               player reads their own job before logging on
  review       afterwards: what worked, written by staff

And players can suggest what to work on next. A suggestion is seen by
staff and by the player who made it, nobody else.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Event, EventLineup, EventSignup, TrainingSuggestion
from services import ServiceError, event_slots

SESSION_TYPES = ("Training", "Theory")
SUGGESTION_STATUSES = ("open", "planned", "done", "declined")
SUGGESTION_LABELS = {"open": "New", "planned": "Planned", "done": "Worked on", "declined": "Not now"}

_MAX_OBJECTIVE = 200
_MAX_PLAN = 3000
_MAX_BRIEF = 300
_MAX_REVIEW = 3000
_MAX_SUGGESTION = 500


def is_session(event: Event) -> bool:
    return event.event_type in SESSION_TYPES


def briefs(event: Event) -> dict[str, str]:
    try:
        data = json.loads(event.role_briefs or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v}


def save_plan(session: Session, event: Event, *, objective: str, plan: str,
              role_briefs: dict[str, str]) -> Event:
    objective, plan = (objective or "").strip(), (plan or "").strip()
    if len(objective) > _MAX_OBJECTIVE:
        raise ServiceError(f"Keep the objective under {_MAX_OBJECTIVE} characters.")
    if len(plan) > _MAX_PLAN:
        raise ServiceError(f"Keep the plan under {_MAX_PLAN:,} characters.")
    slots = event_slots(event)
    clean = {}
    for key, text in role_briefs.items():
        text = (text or "").strip()
        if not text:
            continue
        if key not in slots:
            raise ServiceError("That isn't a position in this session's formation.")
        if len(text) > _MAX_BRIEF:
            raise ServiceError(f"Keep each role brief under {_MAX_BRIEF} characters.")
        clean[key] = text
    event.session_objective = objective or None
    event.session_plan = plan or None
    event.role_briefs = json.dumps(clean) if clean else None
    session.commit()
    return event


def save_review(session: Session, event: Event, text: str, by_name: str,
                now: datetime | None = None) -> Event:
    if event.scheduled_at > (now or datetime.utcnow()):
        raise ServiceError("Review the session after it has happened.")
    text = (text or "").strip()
    if len(text) > _MAX_REVIEW:
        raise ServiceError(f"Keep the review under {_MAX_REVIEW:,} characters.")
    event.session_review = text or None
    event.session_reviewed_by = by_name if text else None
    session.commit()
    return event


def my_position(session: Session, event: Event, discord_id) -> str | None:
    """The slot this player has for the session: their place on a published
    team sheet, else the shirt they claimed when signing up."""
    uid = int(discord_id)
    if event.lineup_published_at:
        row = session.execute(select(EventLineup).where(
            EventLineup.event_id == event.id, EventLineup.discord_user_id == uid)).scalar_one_or_none()
        if row:
            return row.slot_key
    signup = session.execute(select(EventSignup).where(
        EventSignup.event_id == event.id, EventSignup.discord_user_id == uid)).scalar_one_or_none()
    return signup.slot_key if signup and signup.slot_key else None


def plan_view(session: Session, event: Event, discord_id=None) -> dict:
    """What the event page shows of a session's plan."""
    slots = event_slots(event)
    all_briefs = briefs(event)
    mine = my_position(session, event, discord_id) if discord_id else None
    return {
        "objective": event.session_objective,
        "plan": event.session_plan,
        "briefs": [{"key": k, "label": slots.get(k, k), "text": v, "mine": k == mine}
                   for k, v in sorted(all_briefs.items(),
                                      key=lambda kv: list(slots).index(kv[0]) if kv[0] in slots else 99)],
        "my_brief": ({"label": slots.get(mine, mine), "text": all_briefs[mine]}
                     if mine and mine in all_briefs else None),
        "my_slot_label": slots.get(mine) if mine else None,
        "review": event.session_review,
        "reviewed_by": event.session_reviewed_by,
    }


def upcoming_sessions(session: Session, now: datetime | None = None, limit: int = 10) -> list[Event]:
    now = now or datetime.utcnow()
    return list(session.execute(select(Event).where(
        Event.event_type.in_(SESSION_TYPES), Event.scheduled_at >= now)
        .order_by(Event.scheduled_at).limit(limit)).scalars())


def recent_sessions(session: Session, now: datetime | None = None, limit: int = 6) -> list[Event]:
    now = now or datetime.utcnow()
    return list(session.execute(select(Event).where(
        Event.event_type.in_(SESSION_TYPES), Event.scheduled_at < now)
        .order_by(Event.scheduled_at.desc()).limit(limit)).scalars())


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #
def add_suggestion(session: Session, *, discord_id: str, name: str, body: str) -> TrainingSuggestion:
    body = (body or "").strip()
    if not body:
        raise ServiceError("Say what you'd like to work on.")
    if len(body) > _MAX_SUGGESTION:
        raise ServiceError(f"Keep a suggestion under {_MAX_SUGGESTION} characters.")
    row = TrainingSuggestion(discord_id=str(discord_id), display_name=name, body=body)
    session.add(row)
    session.commit()
    return row


def list_suggestions(session: Session, discord_id: str | None = None) -> list[TrainingSuggestion]:
    """Everyone's for staff (discord_id None), else only that player's."""
    q = select(TrainingSuggestion)
    if discord_id is not None:
        q = q.where(TrainingSuggestion.discord_id == str(discord_id))
    order = {s: i for i, s in enumerate(SUGGESTION_STATUSES)}
    rows = list(session.execute(q.order_by(TrainingSuggestion.created_at.desc())).scalars())
    return sorted(rows, key=lambda r: order.get(r.status, 9))


def set_suggestion_status(session: Session, suggestion_id: int, *, status: str, note: str,
                          by_name: str) -> TrainingSuggestion:
    row = session.get(TrainingSuggestion, suggestion_id)
    if row is None:
        raise ServiceError("That suggestion no longer exists.")
    if status not in SUGGESTION_STATUSES:
        raise ServiceError("Pick a status from the list.")
    note = (note or "").strip()
    if len(note) > 300:
        raise ServiceError("Keep the reply under 300 characters.")
    row.status, row.staff_note, row.handled_by = status, note or None, by_name
    session.commit()
    return row
