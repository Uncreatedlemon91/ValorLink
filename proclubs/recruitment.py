"""Recruitment: how somebody goes from joining the Discord server to
being settled in the squad. Staff only -- these are assessments of people
who aren't in the club yet.

  1. Joins the server   new_arrivals() lists recent joiners nobody has
                        dealt with yet; staff start a file or dismiss them.
  2. Prospect           a file: positions, gamertag, where they came from.
  3. On trial           staff write a note after each session they play.
                        A note from a match moves them onto trial, and
                        every note is posted to the recruitment channel
                        (config.RECRUITMENT_CHANNEL_ID).
  4. Offered            a contract offer, sent from their file or from
                        /roster -- either way the file follows it.
  5. Signed             the club confirms the signing on /roster or the
                        file; the contract starts.
  6. Settling in        the ONBOARDING checklist, mostly worked out from
                        the data (contract, gamertag, usual nights, a
                        first goal), plus the steps done by hand.

Or "Not for us" at any point. The Trialist Discord role follows stages
3-4 (role_sync.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
import discord_api
import discord_roster
from models import (AvailabilityPattern, ClubSetting, DevGoal, Event, Player, Prospect,
                    ProspectNote, RosterMove)
from services import ServiceError, get_player_link, live_contract_for

STAGES = ("prospect", "trial", "offered", "signed", "rejected")
STAGE_LABELS = {"prospect": "Prospect", "trial": "On trial", "offered": "Offered",
                "signed": "Signed", "rejected": "Not for us"}
ACTIVE_STAGES = ("prospect", "trial", "offered")

# How far back "new in the server" looks.
ARRIVAL_WINDOW = timedelta(days=30)
# ClubSetting key: Discord ids staff said aren't recruits (a friend, an
# opponent's captain), so they stop showing as new arrivals.
_DISMISSED_KEY = "recruitment:dismissed"
_DISMISSED_MAX = 500

# The settling-in checklist, in order. "auto" steps are read from the
# data and can't be ticked by hand; the rest are ticked on the file.
ONBOARDING = (
    {"key": "contract", "label": "Contract started", "auto": True,
     "hint": "Starts when the signing is confirmed.", "href": "/roster"},
    {"key": "gamertag", "label": "Gamertag linked", "auto": True,
     "hint": "So their match stats and playing time are tracked.", "href": "/squad"},
    {"key": "ea_club", "label": "Invited to the EA club in-game", "auto": False,
     "hint": "Send the invite from Pro Clubs and check they've joined.", "href": None},
    {"key": "welcome", "label": "Welcome message sent", "auto": False,
     "hint": "The button below DMs them the links they'll need.", "href": None},
    {"key": "nights", "label": "Usual nights set", "auto": True,
     "hint": "They set these themselves on Availability.", "href": "/availability"},
    {"key": "goal", "label": "First development goal set", "auto": True,
     "hint": "Set from their player file.", "href": None},
)
MANUAL_STEPS = tuple(s["key"] for s in ONBOARDING if not s["auto"])

_GREEN, _AMBER, _BLUE, _GREY = 0x9CCB64, 0xE39A3B, 0x79AEF7, 0x8A93A6


def _positions(primary: str, secondary: str) -> tuple[str | None, str | None]:
    allowed = set(discord_roster.PITCH_POSITIONS)
    primary, secondary = (primary or "").strip(), (secondary or "").strip()
    if primary and primary not in allowed or secondary and secondary not in allowed:
        raise ServiceError("Pick positions from the list.")
    if secondary and secondary == primary:
        secondary = ""
    return primary or None, secondary or None


def _discord_id(value: str) -> str | None:
    value = (value or "").strip()
    if value and not value.isdigit():
        raise ServiceError("A Discord ID is a number (Developer Mode → Copy User ID).")
    return value or None


def find_by_discord_id(session: Session, discord_id: str | None) -> Prospect | None:
    """The newest file for this Discord account, if there is one."""
    if not discord_id:
        return None
    return session.execute(select(Prospect).where(Prospect.discord_id == str(discord_id))
                           .order_by(Prospect.id.desc())).scalars().first()


def _refuse_duplicate(session: Session, discord_id: str | None, keep: Prospect | None = None) -> None:
    existing = find_by_discord_id(session, discord_id)
    if existing is not None and existing is not keep:
        raise ServiceError(f"{existing.name} already has a file for that Discord account "
                           f"(/recruitment/{existing.id}).")


def add_prospect(session: Session, *, name: str, discord_id: str, gamertag: str, position: str,
                 secondary: str, source: str, added_by: str) -> Prospect:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Give the prospect a name.")
    if len(name) > 60:
        raise ServiceError("Keep the name under 60 characters.")
    did = _discord_id(discord_id)
    _refuse_duplicate(session, did)
    primary, second = _positions(position, secondary)
    row = Prospect(name=name, discord_id=did, gamertag=(gamertag or "").strip()[:64] or None,
                   position=primary, secondary_position=second, source=(source or "").strip()[:120] or None,
                   added_by=added_by)
    session.add(row)
    session.commit()
    return row


def update_prospect(session: Session, prospect: Prospect, *, name: str, discord_id: str, gamertag: str,
                    position: str, secondary: str, source: str) -> Prospect:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Give the prospect a name.")
    if len(name) > 60:
        raise ServiceError("Keep the name under 60 characters.")
    did = _discord_id(discord_id)
    _refuse_duplicate(session, did, keep=prospect)
    prospect.name = name
    prospect.discord_id = did
    prospect.gamertag = (gamertag or "").strip()[:64] or None
    prospect.position, prospect.secondary_position = _positions(position, secondary)
    prospect.source = (source or "").strip()[:120] or None
    session.commit()
    return prospect


def get_prospect(session: Session, prospect_id: int) -> Prospect | None:
    return session.get(Prospect, prospect_id)


def board(session: Session) -> dict[str, list[Prospect]]:
    rows = session.execute(select(Prospect).order_by(Prospect.updated_at.desc())).scalars()
    out = {s: [] for s in STAGES}
    for p in rows:
        out.setdefault(p.stage, []).append(p)
    return out


def summaries(session: Session) -> dict[int, dict]:
    """prospect id -> {"notes": n, "avg": rating or None}, in one query,
    for the board's cards."""
    rows = session.execute(select(ProspectNote.prospect_id, func.count(ProspectNote.id),
                                  func.avg(ProspectNote.rating))
                           .group_by(ProspectNote.prospect_id)).all()
    return {pid: {"notes": n, "avg": round(avg, 1) if avg is not None else None} for pid, n, avg in rows}


def set_stage(session: Session, prospect: Prospect, stage: str) -> Prospect:
    if stage not in STAGES:
        raise ServiceError("Pick a stage from the list.")
    prospect.stage = stage
    if stage == "signed" and prospect.signed_at is None:
        prospect.signed_at = datetime.utcnow()
    session.commit()
    return prospect


# --------------------------------------------------------------------------- #
# Trial notes, and the copy posted to Discord
# --------------------------------------------------------------------------- #
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


def _site(path: str) -> str:
    return f"{config.SITE_BASE_URL}{path}" if config.SITE_BASE_URL else path


def feedback_embed(prospect: Prospect, note: ProspectNote, event: Event | None,
                   avg: float | None, note_count: int) -> dict:
    """The recruitment channel's copy of one trial note."""
    colour = _GREY
    if note.rating:
        colour = _GREEN if note.rating >= 7 else _AMBER if note.rating >= 5 else 0xE31937
    positions = " / ".join(p for p in (prospect.position, prospect.secondary_position) if p)
    fields = [{"name": "Stage", "value": STAGE_LABELS.get(prospect.stage, prospect.stage), "inline": True}]
    if note.rating:
        fields.append({"name": "Rating", "value": f"**{note.rating}/10**", "inline": True})
    if avg is not None and note_count > 1:
        fields.append({"name": "Average so far", "value": f"{avg}/10 over {note_count} notes", "inline": True})
    if event is not None:
        fields.append({"name": "From", "inline": False, "value": (
            f"[{event.title}]({_site(f'/events/{event.id}')}) · "
            f"<t:{int(event.scheduled_at.replace(tzinfo=timezone.utc).timestamp())}:D>")})
    if prospect.discord_id:
        fields.append({"name": "Discord", "value": f"<@{prospect.discord_id}>", "inline": True})
    if prospect.gamertag:
        fields.append({"name": "Gamertag", "value": prospect.gamertag[:64], "inline": True})
    return {
        "title": f"Trial feedback — {prospect.name}"[:256],
        "url": _site(f"/recruitment/{prospect.id}"),
        "color": colour,
        "description": ((f"*{positions}*\n\n" if positions else "") + note.body)[:4000],
        "fields": fields,
        "footer": {"text": f"From {note.author}"},
        "timestamp": (note.created_at or datetime.utcnow()).replace(tzinfo=timezone.utc).isoformat(),
    }


def post_feedback(session: Session, prospect: Prospect, note: ProspectNote) -> str | None:
    """Posts a trial note to the recruitment channel. Returns why it
    didn't go, or None. Never raises: the note is saved either way.

    Nobody is pinged -- the prospect's mention renders as a name, and the
    note's own text can't reach @everyone (allowed_mentions is empty)."""
    if not config.RECRUITMENT_FEEDBACK_ENABLED:
        return None
    notes = notes_for(session, prospect.id)
    event = session.get(Event, note.event_id) if note.event_id else None
    embed = feedback_embed(prospect, note, event, average_rating(notes), len(notes))
    try:
        resp = discord_api.post(f"/channels/{config.RECRUITMENT_CHANNEL_ID}/messages",
                                {"embeds": [embed], "allowed_mentions": {"parse": []}})
    except discord_api.DiscordApiError as exc:
        return str(exc)
    note.discord_message_id = str(resp.json().get("id") or "") or None
    session.commit()
    return None


# --------------------------------------------------------------------------- #
# 1. New in the server
# --------------------------------------------------------------------------- #
def _parse_joined(value) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when


def dismissed_ids(session: Session) -> list[str]:
    row = session.get(ClubSetting, _DISMISSED_KEY)
    try:
        return [str(i) for i in json.loads(row.value)] if row and row.value else []
    except (TypeError, ValueError):
        return []


def dismiss_arrival(session: Session, discord_id: str, by_name: str) -> None:
    did = _discord_id(discord_id)
    if not did:
        raise ServiceError("Pick somebody to dismiss.")
    ids = [i for i in dismissed_ids(session) if i != did] + [did]
    row = session.get(ClubSetting, _DISMISSED_KEY)
    if row is None:
        row = ClubSetting(key=_DISMISSED_KEY)
        session.add(row)
    row.value = json.dumps(ids[-_DISMISSED_MAX:])
    row.updated_by_name = by_name
    session.commit()


def new_arrivals(session: Session, members: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Server members who joined within ARRIVAL_WINDOW and nobody has dealt
    with yet: no file here, no contract, no offer, no club role, not
    dismissed. Newest first. `members` is the raw guild member list."""
    now = now or datetime.utcnow()
    known = set(session.execute(select(Prospect.discord_id).where(Prospect.discord_id.isnot(None))).scalars())
    known |= set(session.execute(select(RosterMove.discord_id)).scalars())
    known |= set(session.execute(select(Player.discord_id).where(Player.club_role.isnot(None))).scalars())
    known |= set(dismissed_ids(session))
    out = []
    for m in members:
        user = m.get("user") or {}
        uid = str(user.get("id") or "")
        if not uid or user.get("bot") or uid in known:
            continue
        joined = _parse_joined(m.get("joined_at"))
        if joined is None or now - joined > ARRIVAL_WINDOW:
            continue
        if live_contract_for(session, uid) is not None:
            continue
        out.append({"id": uid, "name": discord_roster.display_name(m),
                    "username": user.get("username") or "",
                    "avatar_url": discord_roster.avatar_url(m), "joined_at": joined})
    return sorted(out, key=lambda a: a["joined_at"], reverse=True)


def start_file(session: Session, member: dict, *, added_by: str) -> Prospect:
    """A file for somebody picked out of the server's member list."""
    return add_prospect(session, name=member["name"][:60], discord_id=member["id"], gamertag="",
                        position="", secondary="", source="Joined the Discord server", added_by=added_by)


# --------------------------------------------------------------------------- #
# 4-5. The offer, followed from the file
# --------------------------------------------------------------------------- #
def latest_offer(session: Session, prospect: Prospect) -> RosterMove | None:
    """Their most recent player-contract offer, if they have a Discord id."""
    if not prospect.discord_id:
        return None
    return session.execute(select(RosterMove).where(
        RosterMove.discord_id == prospect.discord_id, RosterMove.kind == discord_roster.MOVE_OFFER)
        .order_by(RosterMove.announced_at.desc(), RosterMove.id.desc())).scalars().first()


def offer_state(move: RosterMove | None) -> str | None:
    """None, or: undelivered | waiting | accepted | declined | confirmed."""
    if move is None:
        return None
    if move.confirmed_at:
        return "confirmed"
    if move.response:
        return move.response
    return "waiting" if move.discord_message_id else "undelivered"


def mark_offered(session: Session, discord_id: str) -> Prospect | None:
    """An offer went out to this person: their file moves to Offered."""
    p = find_by_discord_id(session, discord_id)
    if p is not None and p.stage in ("prospect", "trial", "rejected"):
        p.stage = "offered"
        session.commit()
    return p


def mark_signed(session: Session, discord_id: str) -> Prospect | None:
    """The club confirmed their signing: the file moves to Signed and the
    settling-in checklist starts."""
    p = find_by_discord_id(session, discord_id)
    if p is not None and p.stage != "signed":
        p.stage = "signed"
        p.signed_at = p.signed_at or datetime.utcnow()
        session.commit()
    return p


# --------------------------------------------------------------------------- #
# 6. Settling in
# --------------------------------------------------------------------------- #
def _ticked(prospect: Prospect) -> set[str]:
    return {k for k in (prospect.onboarding or "").split(",") if k}


def tick(session: Session, prospect: Prospect, step: str, done: bool) -> None:
    if step not in MANUAL_STEPS:
        raise ServiceError("That step is worked out by the site — it can't be ticked by hand.")
    ticked = _ticked(prospect)
    ticked = ticked | {step} if done else ticked - {step}
    prospect.onboarding = ",".join(k for k in MANUAL_STEPS if k in ticked) or None
    session.commit()


def onboarding(session: Session, prospect: Prospect) -> list[dict]:
    """The checklist with each step's state: [{key, label, hint, href,
    auto, done}]. Steps that need a Discord id are never done without one."""
    did = prospect.discord_id
    ticked = _ticked(prospect)
    auto = {
        "contract": bool(did) and live_contract_for(session, did) is not None,
        "gamertag": bool(did) and get_player_link(session, int(did)) is not None,
        "nights": bool(did) and session.get(AvailabilityPattern, did) is not None,
        "goal": bool(did) and session.execute(select(DevGoal.id).where(DevGoal.discord_id == did)).first() is not None,
    }
    out = []
    for step in ONBOARDING:
        row = dict(step)
        row["done"] = auto[step["key"]] if step["auto"] else step["key"] in ticked
        if step["key"] == "goal" and did:
            row["href"] = f"/players/{did}"
        out.append(row)
    return out


def progress(steps: list[dict]) -> tuple[int, int]:
    return sum(1 for s in steps if s["done"]), len(steps)


def settling_in(session: Session) -> list[tuple[Prospect, tuple[int, int]]]:
    """Signed prospects whose checklist isn't finished, oldest signing first."""
    rows = session.execute(select(Prospect).where(Prospect.stage == "signed")
                           .order_by(Prospect.signed_at)).scalars()
    out = []
    for p in rows:
        done, total = progress(onboarding(session, p))
        if done < total:
            out.append((p, (done, total)))
    return out


def trialists_without_notes(session: Session) -> list[Prospect]:
    """On trial, and nobody has written a word about them."""
    noted = set(session.execute(select(ProspectNote.prospect_id)).scalars())
    return [p for p in session.execute(select(Prospect).where(Prospect.stage == "trial")).scalars()
            if p.id not in noted]


def welcome_dm(prospect: Prospect) -> str:
    return (f"Welcome to {config.SITE_NAME}, {prospect.name}! A few things to get you started:\n"
            f"• Your player file — stats, contract and goals: {_site('/players/me')}\n"
            f"• Set the nights you can usually play: {_site('/availability')}\n"
            f"• Fixtures and sign-ups: {_site('/events')}\n"
            f"• How we play: {_site('/tactics')}\n"
            f"Sign in with this Discord account. Look out for the EA club invite in Pro Clubs.")
