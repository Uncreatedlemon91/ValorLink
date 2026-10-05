"""The trials pipeline: Prospect -> Trial -> Offered -> Signed, or Not for
us. Staff only -- these are assessments of people who aren't in the club
yet. A trial note can name the match it was made in, so a prospect's file
reads as a record of their trials.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import discord_roster
from models import Event, Prospect, ProspectNote
from services import ServiceError

STAGES = ("prospect", "trial", "offered", "signed", "rejected")
STAGE_LABELS = {"prospect": "Prospect", "trial": "On trial", "offered": "Offered",
                "signed": "Signed", "rejected": "Not for us"}
ACTIVE_STAGES = ("prospect", "trial", "offered")


def _positions(primary: str, secondary: str) -> tuple[str | None, str | None]:
    allowed = set(discord_roster.PITCH_POSITIONS)
    primary, secondary = (primary or "").strip(), (secondary or "").strip()
    if primary and primary not in allowed or secondary and secondary not in allowed:
        raise ServiceError("Pick positions from the list.")
    if secondary and secondary == primary:
        secondary = ""
    return primary or None, secondary or None


def add_prospect(session: Session, *, name: str, discord_id: str, gamertag: str, position: str,
                 secondary: str, source: str, added_by: str) -> Prospect:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Give the prospect a name.")
    if len(name) > 60:
        raise ServiceError("Keep the name under 60 characters.")
    discord_id = (discord_id or "").strip()
    if discord_id and not discord_id.isdigit():
        raise ServiceError("A Discord ID is a number (Developer Mode → Copy User ID).")
    primary, second = _positions(position, secondary)
    row = Prospect(name=name, discord_id=discord_id or None, gamertag=(gamertag or "").strip()[:64] or None,
                   position=primary, secondary_position=second, source=(source or "").strip()[:120] or None,
                   added_by=added_by)
    session.add(row)
    session.commit()
    return row


def get_prospect(session: Session, prospect_id: int) -> Prospect | None:
    return session.get(Prospect, prospect_id)


def board(session: Session) -> dict[str, list[Prospect]]:
    rows = session.execute(select(Prospect).order_by(Prospect.updated_at.desc())).scalars()
    out = {s: [] for s in STAGES}
    for p in rows:
        out.setdefault(p.stage, []).append(p)
    return out


def set_stage(session: Session, prospect: Prospect, stage: str) -> Prospect:
    if stage not in STAGES:
        raise ServiceError("Pick a stage from the list.")
    prospect.stage = stage
    session.commit()
    return prospect


def add_note(session: Session, prospect: Prospect, *, body: str, rating, event_id, author: str) -> ProspectNote:
    body = (body or "").strip()
    if not body:
        raise ServiceError("Write the note.")
    if len(body) > 2000:
        raise ServiceError("Keep a note under 2,000 characters.")
    score = None
    if rating not in (None, ""):
        try:
            score = int(rating)
        except (TypeError, ValueError):
            raise ServiceError("Ratings are whole numbers from 1 to 10.")
        if not 1 <= score <= 10:
            raise ServiceError("Ratings are whole numbers from 1 to 10.")
    eid = int(event_id) if str(event_id or "").isdigit() else None
    if eid and session.get(Event, eid) is None:
        raise ServiceError("That match no longer exists.")
    note = ProspectNote(prospect_id=prospect.id, body=body, rating=score, event_id=eid, author=author)
    session.add(note)
    if prospect.stage == "prospect" and eid:
        prospect.stage = "trial"           # a trial note means they've trialled
    session.commit()
    return note


def notes_for(session: Session, prospect_id: int) -> list[tuple[ProspectNote, Event | None]]:
    rows = session.execute(select(ProspectNote).where(ProspectNote.prospect_id == prospect_id)
                           .order_by(ProspectNote.created_at.desc())).scalars()
    out = []
    for n in rows:
        out.append((n, session.get(Event, n.event_id) if n.event_id else None))
    return out


def average_rating(notes: list) -> float | None:
    scores = [n.rating for n, _ in notes if n.rating]
    return round(sum(scores) / len(scores), 1) if scores else None
