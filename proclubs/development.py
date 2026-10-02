"""Development: goals a coach sets a player, and monthly one-to-one reviews.

Unlike coach notes, these are the player's to see -- they're the part of
a player's development they're meant to work on. Visible to the player
and to staff, nobody else.

  goals    A coach sets one (up to MAX_OPEN_GOALS open at once). The player
           updates their progress and adds a note as they go. Staff close
           it as achieved, or drop it.
  reviews  A short record of a monthly one-to-one: how the month went,
           against the goals. One per player per month.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import DevGoal, DevReview
from services import ServiceError

GOAL_AREAS = ("Attacking", "Defending", "Positioning", "Teamwork", "Technique", "Mentality")
GOAL_STATUSES = ("open", "achieved", "dropped")
PROGRESS_STEPS = (0, 25, 50, 75, 100)
MAX_OPEN_GOALS = 3

_MAX_GOAL = 200
_MAX_NOTE = 400
_MAX_REVIEW = 2000


def goals_for(session: Session, discord_id: str) -> list[DevGoal]:
    """Open first (oldest first), then closed (newest first)."""
    rows = list(session.execute(select(DevGoal).where(DevGoal.discord_id == str(discord_id))).scalars())
    open_ = sorted((g for g in rows if g.status == "open"), key=lambda g: g.created_at)
    closed = sorted((g for g in rows if g.status != "open"),
                    key=lambda g: g.closed_at or g.updated_at, reverse=True)
    return open_ + closed


def open_goal_counts(session: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for g in session.execute(select(DevGoal).where(DevGoal.status == "open")).scalars():
        counts[g.discord_id] = counts.get(g.discord_id, 0) + 1
    return counts


def set_goal(session: Session, *, discord_id: str, text: str, area: str, due_on: date | None,
             coach: dict, today: date | None = None) -> DevGoal:
    text = (text or "").strip()
    if not text:
        raise ServiceError("Write the goal.")
    if len(text) > _MAX_GOAL:
        raise ServiceError(f"Keep a goal under {_MAX_GOAL} characters.")
    if area and area not in GOAL_AREAS:
        raise ServiceError("Pick an area from the list.")
    if due_on and due_on < (today or datetime.utcnow().date()):
        raise ServiceError("The target date has already passed.")
    if open_goal_counts(session).get(str(discord_id), 0) >= MAX_OPEN_GOALS:
        raise ServiceError(f"A player can work on {MAX_OPEN_GOALS} goals at once -- close one first.")
    goal = DevGoal(discord_id=str(discord_id), text=text, area=area or None, due_on=due_on,
                   set_by_name=coach.get("name") or "Coach", set_by_id=str(coach.get("id") or ""))
    session.add(goal)
    session.commit()
    return goal


def get_goal(session: Session, goal_id: int) -> DevGoal | None:
    return session.get(DevGoal, goal_id)


def update_progress(session: Session, goal: DevGoal, discord_id, *, progress, note: str) -> DevGoal:
    """The player's own update. Only on their own, open goals."""
    if goal.discord_id != str(discord_id):
        raise ServiceError("You can only update your own goals.")
    if goal.status != "open":
        raise ServiceError("That goal is closed.")
    try:
        progress = int(progress)
    except (TypeError, ValueError):
        raise ServiceError("Pick how far along you are.")
    if progress not in PROGRESS_STEPS:
        raise ServiceError("Pick how far along you are.")
    note = (note or "").strip()
    if len(note) > _MAX_NOTE:
        raise ServiceError(f"Keep your note under {_MAX_NOTE} characters.")
    goal.progress, goal.player_note = progress, note or None
    session.commit()
    return goal


def close_goal(session: Session, goal: DevGoal, *, status: str, by_name: str) -> DevGoal:
    if status not in ("achieved", "dropped", "open"):
        raise ServiceError("Pick achieved, dropped or reopen.")
    if status == "open" and goal.status != "open":
        if open_goal_counts(session).get(goal.discord_id, 0) >= MAX_OPEN_GOALS:
            raise ServiceError(f"They already have {MAX_OPEN_GOALS} open goals.")
    goal.status = status
    goal.closed_at = None if status == "open" else datetime.utcnow()
    goal.closed_by_name = None if status == "open" else by_name
    if status == "achieved":
        goal.progress = 100
    session.commit()
    return goal


def reviews_for(session: Session, discord_id: str) -> list[DevReview]:
    return list(session.execute(select(DevReview).where(DevReview.discord_id == str(discord_id))
                                .order_by(DevReview.month.desc())).scalars())


def save_review(session: Session, *, discord_id: str, month: str, summary: str,
                coach: dict) -> DevReview:
    """One review per player per month; saving again replaces it."""
    try:
        year, mon = (int(x) for x in (month or "").split("-"))
        datetime(year, mon, 1)
    except (ValueError, TypeError):
        raise ServiceError("Pick the month the review is for.")
    summary = (summary or "").strip()
    if not summary:
        raise ServiceError("Write the review.")
    if len(summary) > _MAX_REVIEW:
        raise ServiceError(f"Keep a review under {_MAX_REVIEW:,} characters.")
    key = f"{year}-{mon:02d}"
    row = session.execute(select(DevReview).where(
        DevReview.discord_id == str(discord_id), DevReview.month == key)).scalar_one_or_none()
    if row is None:
        row = DevReview(discord_id=str(discord_id), month=key)
        session.add(row)
    row.summary, row.by_name = summary, coach.get("name") or "Coach"
    session.commit()
    return row


def month_label(key: str) -> str:
    year, mon = (int(x) for x in key.split("-"))
    return datetime(year, mon, 1).strftime("%B %Y")
