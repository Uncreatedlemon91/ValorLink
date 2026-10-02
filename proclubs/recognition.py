"""Recognition: milestones, Player of the Month and the leaderboards.

Milestones are judged against two records: EA's (appearances, goals,
assists, clean sheets -- from the club's recorded matches, via each
player's linked gamertag) and the squad's own (Man of the Match votes,
see matchweek.py). Each is awarded once and announced once by the bot
(notify_poll.py). The very first time milestones are evaluated, whatever
players have already reached is recorded silently: history from before
this feature existed isn't news.

Player of the Month goes to whoever received the most Man of the Match
votes across the month's matches -- votes rather than wins, so a player
who came second three times can beat one lucky win. Level on votes, the
one with more wins takes it; still level, they share it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import matchweek as mw
from models import Event, Milestone, MotmVote, PlayerOfTheMonth

# (key, label, metric, threshold), in the order they're shown.
MILESTONES = [
    ("apps-1", "Debut", "apps", 1),
    ("apps-10", "10 appearances", "apps", 10),
    ("apps-25", "25 appearances", "apps", 25),
    ("apps-50", "50 appearances", "apps", 50),
    ("apps-100", "100 appearances", "apps", 100),
    ("apps-200", "200 appearances", "apps", 200),
    ("goals-1", "First goal", "goals", 1),
    ("goals-10", "10 goals", "goals", 10),
    ("goals-25", "25 goals", "goals", 25),
    ("goals-50", "50 goals", "goals", 50),
    ("goals-100", "100 goals", "goals", 100),
    ("assists-10", "10 assists", "assists", 10),
    ("assists-25", "25 assists", "assists", 25),
    ("assists-50", "50 assists", "assists", 50),
    ("cs-5", "5 clean sheets", "clean_sheets", 5),
    ("cs-10", "10 clean sheets", "clean_sheets", 10),
    ("cs-25", "25 clean sheets", "clean_sheets", 25),
    ("motm-1", "First Man of the Match", "squad_motm", 1),
    ("motm-5", "5 Man of the Match awards", "squad_motm", 5),
    ("motm-10", "10 Man of the Match awards", "squad_motm", 10),
    ("motm-25", "25 Man of the Match awards", "squad_motm", 25),
]
_ORDER = {key: i for i, (key, *_rest) in enumerate(MILESTONES)}
LEADERBOARD_SIZE = 5


def player_metrics(totals: dict, links: dict[int, str], motm: dict[str, int],
                   people: list[str]) -> dict[str, dict]:
    """discord_id -> {apps, goals, assists, clean_sheets, squad_motm}."""
    out = {}
    for pid in people:
        tag = links.get(int(pid)) if pid.isdigit() else None
        t = totals.get(tag.casefold(), {}) if tag else {}
        out[pid] = {"apps": t.get("apps", 0), "goals": t.get("goals", 0),
                    "assists": t.get("assists", 0), "clean_sheets": t.get("clean_sheets", 0),
                    "squad_motm": motm.get(pid, 0)}
    return out


def evaluate(session: Session, metrics: dict[str, dict],
             now: datetime | None = None) -> list[Milestone]:
    """Records every milestone reached and not yet recorded. Returns the
    new rows. On the first run ever they're stored already announced."""
    now = now or datetime.utcnow()
    # A marker, not "no milestones yet": a club starting from nothing
    # should still hear about its first debut.
    first_run = not mw.already_sent(session, "milestones", "initialised")
    have = {(m.discord_id, m.key) for m in session.execute(select(Milestone)).scalars()}
    new = []
    for pid, values in metrics.items():
        for key, label, metric, threshold in MILESTONES:
            if values.get(metric, 0) >= threshold and (pid, key) not in have:
                row = Milestone(discord_id=pid, key=key, label=label, achieved_at=now,
                                announced_at=now if first_run else None)
                session.add(row)
                new.append(row)
    session.commit()
    if first_run:
        mw.mark_sent(session, "milestones", "initialised")
    return new


def unannounced(session: Session) -> list[Milestone]:
    return list(session.execute(select(Milestone).where(Milestone.announced_at.is_(None))
                                .order_by(Milestone.achieved_at, Milestone.id)).scalars())


def mark_announced(session: Session, rows: list, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    for row in rows:
        row.announced_at = now
    session.commit()


def milestones_for(session: Session, discord_id: str) -> list[Milestone]:
    rows = session.execute(select(Milestone).where(Milestone.discord_id == str(discord_id))).scalars()
    return sorted(rows, key=lambda m: _ORDER.get(m.key, 99))


# --------------------------------------------------------------------------- #
# Player of the Month
# --------------------------------------------------------------------------- #
def month_key(when: datetime) -> str:
    return f"{when.year}-{when.month:02d}"


def month_bounds(key: str) -> tuple[datetime, datetime]:
    year, month = (int(x) for x in key.split("-"))
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    return start, end


def previous_month(now: datetime) -> str:
    return month_key(datetime(now.year - (now.month == 1), (now.month - 2) % 12 + 1, 1))


def month_label(key: str) -> str:
    return month_bounds(key)[0].strftime("%B %Y")


def votes_received(session: Session, start: datetime, end: datetime) -> dict[str, int]:
    rows = session.execute(
        select(MotmVote.nominee_id, func.count(MotmVote.id))
        .join(Event, Event.id == MotmVote.event_id)
        .where(Event.scheduled_at >= start, Event.scheduled_at < end)
        .group_by(MotmVote.nominee_id)).all()
    return dict(rows)


def award_month(session: Session, key: str, names: dict[str, str],
                now: datetime | None = None) -> PlayerOfTheMonth | None:
    """Awards a finished month, once. None if it isn't over or nobody
    received a vote."""
    now = now or datetime.utcnow()
    start, end = month_bounds(key)
    if now < end:
        return None
    existing = session.get(PlayerOfTheMonth, key)
    if existing:
        return existing
    votes = votes_received(session, start, end)
    if not votes:
        return None
    top = max(votes.values())
    leaders = [pid for pid, n in votes.items() if n == top]
    if len(leaders) > 1:
        wins = mw.motm_wins(session, since=start, until=end)
        best = max(wins.get(pid, 0) for pid in leaders)
        leaders = [pid for pid in leaders if wins.get(pid, 0) == best]
    leaders.sort()
    row = PlayerOfTheMonth(month=key, discord_ids=",".join(leaders),
                           names=" & ".join(names.get(pid, "A teammate") for pid in leaders),
                           votes=top, awarded_at=now)
    session.add(row)
    session.commit()
    return row


def months_won(session: Session, discord_id: str) -> list[PlayerOfTheMonth]:
    rows = session.execute(select(PlayerOfTheMonth).order_by(PlayerOfTheMonth.month.desc())).scalars()
    return [r for r in rows if str(discord_id) in r.discord_ids.split(",")]


def latest_award(session: Session) -> PlayerOfTheMonth | None:
    return session.execute(select(PlayerOfTheMonth).order_by(PlayerOfTheMonth.month.desc())
                           .limit(1)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Leaderboards
# --------------------------------------------------------------------------- #
def leaderboards(people: list[dict], usage: dict, links: dict[int, str], motm: dict[str, int],
                 attendance: dict[int, dict]) -> list[dict]:
    """Top players by form, goals, assists, squad Man of the Match awards
    and attendance -- [{title, unit, rows: [{id, name, value}]}]. A player
    with nothing to show in a board isn't listed in it."""
    def row(p, value):
        return {"id": p["id"], "name": p["name"], "value": value}

    boards = {"Form": [], "Goals": [], "Assists": [], "Man of the Match": [], "Turns up": []}
    for p in people:
        tag = links.get(int(p["id"])) if p["id"].isdigit() else None
        u = usage["players"].get(tag.casefold()) if tag else None
        if u and u.get("form") is not None:
            boards["Form"].append(row(p, u["form"]))
        if u and u.get("goals"):
            boards["Goals"].append(row(p, u["goals"]))
        if u and u.get("assists"):
            boards["Assists"].append(row(p, u["assists"]))
        if motm.get(p["id"]):
            boards["Man of the Match"].append(row(p, motm[p["id"]]))
        rec = attendance.get(int(p["id"])) if p["id"].isdigit() else None
        if rec and rec.get("rate") is not None:
            boards["Turns up"].append(row(p, rec["rate"]))
    units = {"Form": "avg rating, last 5", "Goals": "recorded matches", "Assists": "recorded matches",
             "Man of the Match": "squad votes", "Turns up": "% of marked events"}
    out = []
    for title, rows in boards.items():
        rows.sort(key=lambda r: (-r["value"], r["name"].casefold()))
        out.append({"title": title, "unit": units[title], "rows": rows[:LEADERBOARD_SIZE]})
    return out
