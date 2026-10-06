"""The match-week loop: availability -> sign-up with position preferences
-> the team sheet -> after the match, ratings, the Man of the Match vote
and the match report.

Rules only -- reading and writing rows, and deciding what's allowed. The
routes are in matchweek_routes.py, and what the bot posts is in
discord_notify.py, sent by notify_poll.py on a timer.

Who counts as having played a match (who may rate themselves and vote):
the published team sheet if there is one, otherwise everyone who signed up
as going. Pro Clubs has no reliable record of who started, so this is the
club's own record of who was picked.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import discord_roster
import roles
from models import (AvailabilityAway, AvailabilityPattern, Contract, Event, EventLineup,
                    EventSignup, MatchRating, MotmVote, Notification, Player)
from services import ServiceError, event_slots, live_contracts

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Fixtures that get a team sheet, a vote and a report. Community nights
# and training sessions don't.
MATCH_TYPES = ("Match", "Scrim", "Tournament")

# The vote opens this long after kick-off -- a match night is usually
# several games -- and stays open this long, unless the report is
# published first.
VOTE_OPENS_AFTER = timedelta(hours=2)
VOTE_WINDOW = timedelta(hours=48)

# How far ahead the availability grid looks, and the longest away period
# anybody can enter in one go.
GRID_DAYS = 14
MAX_AWAY_DAYS = 120

RATING_MIN, RATING_MAX = 1, 10
RATING_WORDS = {10: "Unplayable", 9: "Outstanding", 8: "Very good", 7: "Good", 6: "Solid",
                5: "Average", 4: "Off the pace", 3: "Poor", 2: "Very poor", 1: "Shocker"}


# --------------------------------------------------------------------------- #
# The squad, as the match week sees it
# --------------------------------------------------------------------------- #
def squad_people(session: Session) -> list[dict]:
    """Everyone in the squad: contracted players by status, then staff who
    aren't also under contract. {id, name, avatar, status, position,
    secondary, club_role}."""
    order = {s: i for i, s in enumerate(roles.SQUAD_STATUSES)}
    people: dict[str, dict] = {}
    for c in sorted(live_contracts(session),
                    key=lambda c: (order.get(c.squad_status, 9), c.display_name.casefold())):
        people[c.discord_id] = {
            "id": c.discord_id, "name": c.display_name, "avatar": c.avatar_url,
            "status": c.squad_status, "position": c.position,
            "secondary": c.secondary_position, "club_role": None,
        }
    for p in session.execute(select(Player).where(Player.club_role.is_not(None))).scalars():
        if p.discord_id in people:
            people[p.discord_id]["club_role"] = p.club_role
        else:
            people[p.discord_id] = {
                "id": p.discord_id, "name": p.display_name, "avatar": p.avatar_url,
                "status": None, "position": None, "secondary": None, "club_role": p.club_role,
            }
    return list(people.values())


def contracted_ids(session: Session) -> list[str]:
    return [c.discord_id for c in live_contracts(session)]


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def get_pattern(session: Session, discord_id: str) -> AvailabilityPattern | None:
    return session.get(AvailabilityPattern, str(discord_id))


def set_pattern(session: Session, discord_id: str, days: list[int], note: str = "") -> AvailabilityPattern:
    if any(d not in range(7) for d in days):
        raise ServiceError("Those aren't days of the week.")
    note = (note or "").strip()
    if len(note) > 140:
        raise ServiceError("Keep the note under 140 characters.")
    bits = "".join("1" if d in days else "0" for d in range(7))
    pattern = get_pattern(session, discord_id)
    if pattern is None:
        pattern = AvailabilityPattern(discord_id=str(discord_id))
        session.add(pattern)
    pattern.days, pattern.note = bits, note or None
    session.commit()
    return pattern


def list_away(session: Session, discord_id: str | None = None, *,
              from_day: date | None = None) -> list[AvailabilityAway]:
    q = select(AvailabilityAway)
    if discord_id is not None:
        q = q.where(AvailabilityAway.discord_id == str(discord_id))
    if from_day is not None:
        q = q.where(AvailabilityAway.ends_on >= from_day)
    return list(session.execute(q.order_by(AvailabilityAway.starts_on)).scalars())


def add_away(session: Session, discord_id: str, starts_on: date, ends_on: date,
             note: str = "", today: date | None = None) -> AvailabilityAway:
    today = today or datetime.utcnow().date()
    if ends_on < starts_on:
        raise ServiceError("The last day can't be before the first.")
    if ends_on < today:
        raise ServiceError("Those dates have already passed.")
    if (ends_on - starts_on).days >= MAX_AWAY_DAYS:
        raise ServiceError(f"Enter at most {MAX_AWAY_DAYS} days at a time.")
    note = (note or "").strip()[:140]
    away = AvailabilityAway(discord_id=str(discord_id), starts_on=starts_on,
                            ends_on=ends_on, note=note or None)
    session.add(away)
    session.commit()
    return away


def delete_away(session: Session, away_id: int, discord_id: str) -> None:
    away = session.get(AvailabilityAway, away_id)
    if away is None or away.discord_id != str(discord_id):
        raise ServiceError("That away period isn't yours to remove.")
    session.delete(away)
    session.commit()


def day_state(pattern: AvailabilityPattern | None, aways: list, day: date) -> str:
    """free, busy, away or unknown (no usual nights set)."""
    if any(a.starts_on <= day <= a.ends_on for a in aways):
        return "away"
    if pattern is None:
        return "unknown"
    return "free" if pattern.days[day.weekday()] == "1" else "busy"


def availability_grid(session: Session, people: list[dict], start: date,
                      days: int = GRID_DAYS) -> dict:
    """Who can play on each of the next `days` nights.

    A fixture's own answers win over the pattern on its day: a player
    marked "free on Tuesdays" who said they can't make this Tuesday's match
    is out, not free.
    """
    span = [start + timedelta(days=i) for i in range(days)]
    ids = [p["id"] for p in people]
    patterns = {p.discord_id: p for p in session.execute(
        select(AvailabilityPattern).where(AvailabilityPattern.discord_id.in_(ids))).scalars()}
    aways: dict[str, list] = {}
    for a in list_away(session, from_day=start):
        aways.setdefault(a.discord_id, []).append(a)

    end = datetime.combine(span[-1] + timedelta(days=1), datetime.min.time())
    events = list(session.execute(
        select(Event).where(Event.scheduled_at >= datetime.combine(start, datetime.min.time()),
                            Event.scheduled_at < end).order_by(Event.scheduled_at)).scalars())
    events_on: dict[date, list] = {}
    for e in events:
        events_on.setdefault(e.scheduled_at.date(), []).append(e)
    answers: dict[tuple[int, int], str] = {}
    if events:
        for s in session.execute(select(EventSignup).where(
                EventSignup.event_id.in_([e.id for e in events]))).scalars():
            answers[(s.event_id, s.discord_user_id)] = s.status

    rows, counts = [], [0] * days
    for person in people:
        pattern = patterns.get(person["id"])
        cells = []
        for i, day in enumerate(span):
            state = day_state(pattern, aways.get(person["id"], []), day)
            answer = None
            for e in events_on.get(day, []):
                answer = answers.get((e.id, int(person["id"]))) or answer
            if answer == "out":
                state = "away" if state == "away" else "out"
            elif answer in ("going", "maybe"):
                state = answer
            if state in ("free", "going"):
                counts[i] += 1
            cells.append({"state": state, "answer": answer})
        rows.append({**person, "cells": cells, "pattern_set": pattern is not None})
    return {"days": span, "rows": rows, "counts": counts, "events_on": events_on}


def free_on(session: Session, discord_ids: list[str], day: date) -> dict[str, str]:
    """discord_id -> day_state for one night, for the team sheet."""
    patterns = {p.discord_id: p for p in session.execute(
        select(AvailabilityPattern).where(AvailabilityPattern.discord_id.in_(discord_ids))).scalars()}
    aways: dict[str, list] = {}
    for a in list_away(session, from_day=day):
        aways.setdefault(a.discord_id, []).append(a)
    return {i: day_state(patterns.get(i), aways.get(i, []), day) for i in discord_ids}


# --------------------------------------------------------------------------- #
# Position preferences
# --------------------------------------------------------------------------- #
def set_preferences(session: Session, event: Event, discord_user_id: int,
                    prefs: list[str]) -> EventSignup:
    signup = session.execute(select(EventSignup).where(
        EventSignup.event_id == event.id,
        EventSignup.discord_user_id == discord_user_id)).scalar_one_or_none()
    if signup is None or signup.status == "out":
        raise ServiceError("Sign up first, then say where you'd like to play.")
    chosen = [p.strip() for p in prefs if p and p.strip()]
    allowed = set(discord_roster.PITCH_POSITIONS)
    if any(p not in allowed for p in chosen):
        raise ServiceError("Pick positions from the list.")
    if len(set(chosen)) != len(chosen):
        raise ServiceError("Each choice has to be a different position.")
    chosen = (chosen + [None, None, None])[:3]
    signup.pref_1, signup.pref_2, signup.pref_3 = chosen
    session.commit()
    return signup


def preferences(signup: EventSignup | None) -> list[str]:
    if signup is None:
        return []
    return [p for p in (signup.pref_1, signup.pref_2, signup.pref_3) if p]


# --------------------------------------------------------------------------- #
# The team sheet
# --------------------------------------------------------------------------- #
def is_match(event: Event) -> bool:
    return event.event_type in MATCH_TYPES


def lineup_for(session: Session, event_id: int) -> dict[str, EventLineup]:
    return {row.slot_key: row for row in session.execute(
        select(EventLineup).where(EventLineup.event_id == event_id)).scalars()}


def draft_lineup(session: Session, event: Event) -> dict[str, str]:
    """slot_key -> discord id to show in the editor: the saved sheet, or,
    before anything is saved, the shirts players claimed when signing up."""
    saved = lineup_for(session, event.id)
    if saved:
        return {k: str(v.discord_user_id) for k, v in saved.items()}
    claims = session.execute(select(EventSignup).where(
        EventSignup.event_id == event.id, EventSignup.slot_key.is_not(None),
        EventSignup.status == "going")).scalars()
    slots = event_slots(event)
    return {s.slot_key: str(s.discord_user_id) for s in claims if s.slot_key in slots}


def team_sheet_candidates(session: Session, event: Event, usage: dict | None = None,
                          links: dict[int, str] | None = None) -> list[dict]:
    """Everyone the coach might pick: the squad plus anybody else who
    signed up, each with their answer, preferences, natural positions,
    form and whether they're free that night. Going first, then maybe,
    then no answer, then out."""
    usage = usage or {"players": {}}
    links = links or {}
    signups = {str(s.discord_user_id): s for s in session.execute(
        select(EventSignup).where(EventSignup.event_id == event.id)).scalars()}
    people = {p["id"]: p for p in squad_people(session)}
    for uid, s in signups.items():
        if uid not in people and s.status != "out":
            people[uid] = {"id": uid, "name": s.discord_name, "avatar": None, "status": None,
                           "position": None, "secondary": None, "club_role": None}
    free = free_on(session, list(people), event.scheduled_at.date())
    rank = {"going": 0, "maybe": 1, None: 2, "out": 3}
    out = []
    for uid, person in people.items():
        s = signups.get(uid)
        gamertag = links.get(int(uid)) if uid.isdigit() else None
        u = usage["players"].get(gamertag.casefold()) if gamertag else None
        out.append({
            **person, "answer": s.status if s else None, "prefs": preferences(s),
            "claimed": s.slot_key if s else None, "free": free.get(uid, "unknown"),
            "form": u["form"] if u else None, "apps": u["apps_window"] if u else None,
        })
    out.sort(key=lambda c: (rank.get(c["answer"], 2), c["name"].casefold()))
    return out


def save_lineup(session: Session, event: Event, picks: dict[str, str],
                names: dict[str, str]) -> dict[str, EventLineup]:
    """Replaces the team sheet. `picks` is slot_key -> discord id ("" for
    empty). Nobody can be picked twice, and only real slots count."""
    slots = event_slots(event)
    if not slots:
        raise ServiceError("Give the event a formation before picking a team sheet.")
    chosen = {k: v for k, v in picks.items() if v}
    if any(k not in slots for k in chosen):
        raise ServiceError("That isn't a position in this event's formation.")
    seen: dict[str, str] = {}
    for key, uid in chosen.items():
        if not uid.isdigit():
            raise ServiceError("Pick players from the list.")
        if uid in seen:
            raise ServiceError(f"{names.get(uid, 'A player')} is picked twice "
                               f"({slots[seen[uid]]} and {slots[key]}).")
        seen[uid] = key
    session.execute(delete(EventLineup).where(EventLineup.event_id == event.id))
    for key, uid in chosen.items():
        session.add(EventLineup(event_id=event.id, slot_key=key, discord_user_id=int(uid),
                                display_name=names.get(uid) or f"Player {uid}"))
    session.commit()
    return lineup_for(session, event.id)


def mark_lineup_published(session: Session, event: Event, message_id: str | None) -> None:
    event.lineup_published_at = datetime.utcnow()
    if message_id:
        event.lineup_message_id = message_id
    session.commit()


def my_shirt(session: Session, event: Event, discord_user_id) -> str | None:
    """The slot this player was picked for, if the sheet is published."""
    if not event.lineup_published_at:
        return None
    for key, row in lineup_for(session, event.id).items():
        if row.discord_user_id == int(discord_user_id):
            return key
    return None


# --------------------------------------------------------------------------- #
# After the match: who played, ratings, the vote, the report
# --------------------------------------------------------------------------- #
def participants(session: Session, event: Event) -> list[dict]:
    """[{id, name}] -- the published sheet, or the players who went."""
    if event.lineup_published_at:
        rows = lineup_for(session, event.id).values()
        return [{"id": str(r.discord_user_id), "name": r.display_name}
                for r in sorted(rows, key=lambda r: r.slot_key)]
    signups = session.execute(select(EventSignup).where(
        EventSignup.event_id == event.id, EventSignup.status == "going")
        .order_by(EventSignup.discord_name)).scalars()
    return [{"id": str(s.discord_user_id), "name": s.discord_name} for s in signups]


def is_participant(session: Session, event: Event, discord_id) -> bool:
    return any(p["id"] == str(discord_id) for p in participants(session, event))


def vote_state(event: Event, now: datetime | None = None) -> str:
    """not_yet, open or closed."""
    now = now or datetime.utcnow()
    if not event.vote_opened_at:
        return "not_yet"
    if event.review_published_at or (event.vote_closes_at and now >= event.vote_closes_at):
        return "closed"
    return "open"


def vote_due(event: Event, now: datetime | None = None) -> bool:
    """Should the notifier open this match's vote now?"""
    now = now or datetime.utcnow()
    return (is_match(event) and not event.vote_opened_at and not event.review_published_at
            and event.scheduled_at + VOTE_OPENS_AFTER <= now
            and event.scheduled_at + VOTE_OPENS_AFTER + VOTE_WINDOW > now)


def open_vote(session: Session, event: Event, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    if not is_match(event):
        raise ServiceError("Only matches have a Man of the Match vote.")
    if event.scheduled_at > now:
        raise ServiceError("The vote opens after kick-off.")
    if event.vote_opened_at:
        return
    event.vote_opened_at = now
    event.vote_closes_at = now + VOTE_WINDOW
    session.commit()


def _require_open_and_participant(session: Session, event: Event, discord_id) -> None:
    if vote_state(event) != "open":
        raise ServiceError("Voting isn't open for this match.")
    if not is_participant(session, event, discord_id):
        raise ServiceError("Only players who were in the squad for this match can vote and rate.")


def cast_vote(session: Session, event: Event, voter_id, nominee_id) -> MotmVote:
    _require_open_and_participant(session, event, voter_id)
    if str(voter_id) == str(nominee_id):
        raise ServiceError("You can't vote for yourself.")
    if not is_participant(session, event, nominee_id):
        raise ServiceError("Vote for somebody who played.")
    vote = session.execute(select(MotmVote).where(
        MotmVote.event_id == event.id, MotmVote.voter_id == str(voter_id))).scalar_one_or_none()
    if vote is None:
        vote = MotmVote(event_id=event.id, voter_id=str(voter_id), nominee_id=str(nominee_id))
        session.add(vote)
    else:
        vote.nominee_id = str(nominee_id)
    session.commit()
    return vote


def _rating_row(session: Session, event: Event, discord_id: str, name: str) -> MatchRating:
    row = session.execute(select(MatchRating).where(
        MatchRating.event_id == event.id, MatchRating.discord_id == str(discord_id))).scalar_one_or_none()
    if row is None:
        row = MatchRating(event_id=event.id, discord_id=str(discord_id), display_name=name)
        session.add(row)
    return row


def _parse_rating(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ServiceError("Ratings are whole numbers from 1 to 10.")
    if not RATING_MIN <= n <= RATING_MAX:
        raise ServiceError("Ratings are whole numbers from 1 to 10.")
    return n


def set_self_rating(session: Session, event: Event, discord_id, name: str, rating) -> MatchRating:
    _require_open_and_participant(session, event, discord_id)
    row = _rating_row(session, event, str(discord_id), name)
    row.self_rating = _parse_rating(rating)
    session.commit()
    return row


def set_coach_ratings(session: Session, event: Event, ratings: dict[str, tuple],
                      names: dict[str, str], coach_name: str) -> None:
    """ratings: discord_id -> (rating or "", comment)."""
    played = {p["id"] for p in participants(session, event)}
    for uid, (rating, comment) in ratings.items():
        if uid not in played:
            continue
        value = _parse_rating(rating)
        comment = (comment or "").strip()
        if len(comment) > 500:
            raise ServiceError("Keep each comment under 500 characters.")
        if value is None and not comment:
            continue
        row = _rating_row(session, event, uid, names.get(uid, f"Player {uid}"))
        row.coach_rating, row.coach_comment, row.coach_name = value, comment or None, coach_name
    session.commit()


def ratings_for(session: Session, event_id: int) -> dict[str, MatchRating]:
    return {r.discord_id: r for r in session.execute(
        select(MatchRating).where(MatchRating.event_id == event_id)).scalars()}


def my_vote(session: Session, event_id: int, voter_id) -> str | None:
    return session.execute(select(MotmVote.nominee_id).where(
        MotmVote.event_id == event_id, MotmVote.voter_id == str(voter_id))).scalar_one_or_none()


def motm_result(session: Session, event_id: int) -> dict:
    """{"votes": {id: n}, "total": n, "winners": [ids]} -- a tie names
    everyone level at the top."""
    counts = dict(session.execute(
        select(MotmVote.nominee_id, func.count(MotmVote.id))
        .where(MotmVote.event_id == event_id).group_by(MotmVote.nominee_id)).all())
    top = max(counts.values(), default=0)
    return {"votes": counts, "total": sum(counts.values()),
            "winners": sorted(k for k, v in counts.items() if v == top and top > 0)}


def ea_matches_near(event: Event, history: list[dict]) -> list[dict]:
    """EA's recorded matches from around kick-off to five hours after --
    the games of this match night -- for the night's record (night_summary)."""
    start = event.scheduled_at - timedelta(minutes=30)
    end = event.scheduled_at + timedelta(hours=5)
    out = []
    for m in history:
        if not m.get("played_at"):
            continue
        played = datetime.fromtimestamp(m["played_at"], tz=timezone.utc).replace(tzinfo=None)
        if start <= played <= end:
            out.append({**m, "played": played})
    return sorted(out, key=lambda m: m["played"])


def night_summary(near: list[dict]) -> dict | None:
    """The match night in one line: wins, draws and losses, and the goal
    difference, from EA's matches (see ea_matches_near). None when EA
    recorded nothing; matches without both scores are left out."""
    games = []
    for m in near:
        us, opp = m.get("us_score"), m.get("opp_score")
        if us is None or opp is None:
            continue
        games.append({**m, "letter": "W" if us > opp else "L" if us < opp else "D"})
    if not games:
        return None
    wins, draws, losses = (sum(g["letter"] == k for g in games) for k in "WDL")
    gf, ga = sum(g["us_score"] for g in games), sum(g["opp_score"] for g in games)
    gd = gf - ga
    record = " ".join(f"{n}{k}" for n, k in ((wins, "W"), (draws, "D"), (losses, "L")) if n)
    return {"games": games, "wins": wins, "draws": draws, "losses": losses, "gf": gf, "ga": ga,
            "gd": gd, "gd_text": f"+{gd}" if gd > 0 else f"{gd}".replace("-", "\u2212"),
            "record": record}


def result_text(us: int | None, opp: int | None) -> str | None:
    if us is None or opp is None:
        return None
    letter = "W" if us > opp else "L" if us < opp else "D"
    return f"{letter} {us}-{opp}"


def save_review(session: Session, event: Event, *, notes: str, clips: str) -> Event:
    notes = (notes or "").strip()
    if len(notes) > 3000:
        raise ServiceError("Keep the key moments under 3,000 characters.")
    lines = [line.strip() for line in (clips or "").splitlines() if line.strip()]
    if any(not line.startswith(("https://", "http://")) for line in lines):
        raise ServiceError("Clips are links, one per line.")
    event.review_notes, event.review_clips = notes or None, "\n".join(lines[:10]) or None
    session.commit()
    return event


def publish_review(session: Session, event: Event, by_name: str,
                   now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    if event.scheduled_at > now:
        raise ServiceError("Publish the report after the match.")
    event.review_published_at, event.review_published_by = now, by_name
    if event.vote_opened_at and (not event.vote_closes_at or event.vote_closes_at > now):
        event.vote_closes_at = now
    session.commit()


def clip_links(event: Event) -> list[str]:
    return [line for line in (event.review_clips or "").splitlines() if line]


def ratings_history(session: Session, discord_id: str, limit: int = 10) -> list[dict]:
    """A player's own record, newest first: [{event, rating}] for matches
    they have a rating row for."""
    rows = session.execute(
        select(MatchRating, Event).join(Event, Event.id == MatchRating.event_id)
        .where(MatchRating.discord_id == str(discord_id))
        .order_by(Event.scheduled_at.desc()).limit(limit)).all()
    return [{"rating": r, "event": e} for r, e in rows]


def motm_wins(session: Session, *, since: datetime | None = None,
              until: datetime | None = None) -> dict[str, int]:
    """discord_id -> matches they were voted Man of the Match in (ties
    count for everyone level), over closed votes in the period."""
    q = select(Event).where(Event.vote_opened_at.is_not(None))
    if since:
        q = q.where(Event.scheduled_at >= since)
    if until:
        q = q.where(Event.scheduled_at < until)
    wins: dict[str, int] = {}
    now = datetime.utcnow()
    for event in session.execute(q).scalars():
        if vote_state(event, now) != "closed":
            continue
        for uid in motm_result(session, event.id)["winners"]:
            wins[uid] = wins.get(uid, 0) + 1
    return wins


# --------------------------------------------------------------------------- #
# Notifications sent once
# --------------------------------------------------------------------------- #
def already_sent(session: Session, kind: str, key: str) -> bool:
    return session.execute(select(Notification.id).where(
        Notification.kind == kind, Notification.key == key)).first() is not None


def mark_sent(session: Session, kind: str, key: str, ok: bool = True, detail: str | None = None) -> None:
    session.add(Notification(kind=kind, key=key, ok=ok, detail=(detail or "")[:300] or None))
    session.commit()


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def players_without_pattern(session: Session) -> list[Contract]:
    set_ids = set(session.execute(select(AvailabilityPattern.discord_id)).scalars())
    return [c for c in live_contracts(session) if c.discord_id not in set_ids]


def unanswered(session: Session, event: Event) -> list[Contract]:
    answered = {str(i) for i in session.execute(select(EventSignup.discord_user_id).where(
        EventSignup.event_id == event.id)).scalars()}
    return [c for c in live_contracts(session) if c.discord_id not in answered]


def events_needing_reminder(session: Session, now: datetime | None = None,
                            ahead: timedelta = timedelta(hours=24)) -> list[Event]:
    now = now or datetime.utcnow()
    return list(session.execute(select(Event).where(
        Event.scheduled_at > now, Event.scheduled_at <= now + ahead,
        Event.signups_open.is_(True))).scalars())


def events_due_a_vote(session: Session, now: datetime | None = None) -> list[Event]:
    now = now or datetime.utcnow()
    recent = session.execute(select(Event).where(
        Event.vote_opened_at.is_(None),
        Event.scheduled_at <= now - VOTE_OPENS_AFTER,
        Event.scheduled_at > now - VOTE_OPENS_AFTER - VOTE_WINDOW)).scalars()
    return [e for e in recent if vote_due(e, now)]
