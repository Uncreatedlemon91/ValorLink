"""One-shot run of the bot's scheduled messages. Every job records what it
sent (matchweek.mark_sent), so running this as often as you like sends
each message once.

Jobs:
  availability   Mondays from 15:00 UTC: DM contracted players who have
                 never set their usual nights.
  reminders      In the 24 hours before a fixture: DM contracted players
                 who haven't answered it.
  votes          Two hours after kick-off: open the Man of the Match vote
                 and post it where the match was announced.

  milestones     Award milestones newly reached (recognition.py) and post
                 them together.
  player_of_the_month   From the 1st: award last month and post it.

Run on a schedule via systemd (deploy/proclubs-notify-poll.service +
.timer), like the other pollers. Run it manually to test:
python notify_poll.py
"""
from __future__ import annotations

from datetime import datetime

import config
import discord_notify
import matchweek as mw
from database import get_session, init_db

AVAILABILITY_DAY = 0        # Monday
AVAILABILITY_FROM_HOUR = 15  # UTC: mid-morning across the Americas


def _dm_once(session, kind: str, key: str, user_id: str, content: str) -> bool:
    """DMs one person once. A failure is recorded too, so a closed inbox
    isn't retried every ten minutes forever."""
    if mw.already_sent(session, kind, key):
        return False
    try:
        discord_notify.send_dm(user_id, content)
    except discord_notify.DiscordApiError as exc:
        mw.mark_sent(session, kind, key, ok=False, detail=str(exc))
        return False
    mw.mark_sent(session, kind, key)
    return True


def availability_nudges(session, now: datetime) -> int:
    if not config.NOTIFY_DMS or now.weekday() != AVAILABILITY_DAY or now.hour < AVAILABILITY_FROM_HOUR:
        return 0
    week = mw.iso_week(now.date())
    sent = 0
    for c in mw.players_without_pattern(session):
        sent += _dm_once(session, "availability", f"{week}:{c.discord_id}", c.discord_id,
                         discord_notify.availability_dm())
    return sent


def event_reminders(session, now: datetime) -> int:
    if not config.NOTIFY_DMS:
        return 0
    sent = 0
    for event in mw.events_needing_reminder(session, now):
        for c in mw.unanswered(session, event):
            sent += _dm_once(session, "remind", f"{event.id}:{c.discord_id}", c.discord_id,
                             discord_notify.reminder_dm(event))
    return sent


def open_votes(session, now: datetime) -> int:
    import matchweek_routes  # the post is shared with the staff button

    opened = 0
    for event in mw.events_due_a_vote(session, now):
        if not mw.participants(session, event):
            continue
        mw.open_vote(session, event, now)
        problem = matchweek_routes.post_vote(session, event)
        if problem:
            print(f"event {event.id}: vote opened, but the post failed: {problem}")
        opened += 1
    return opened


def _names(session) -> dict[str, str]:
    from models import Player
    from sqlalchemy import select
    return {p.discord_id: p.display_name for p in session.execute(select(Player)).scalars()}


def milestones(session, now: datetime) -> int:
    """Awards milestones reached since the last run and posts the new ones
    together. Unposted ones stay unposted until a post succeeds."""
    import db
    import recognition
    import services
    from models import Player
    from sqlalchemy import select

    people = [p.discord_id for p in session.execute(select(Player)).scalars()]
    totals = db.career_totals(config.CLUB_PLATFORM, str(config.CLUB_ID)) if config.CLUB_ID else {}
    links = services.player_links_for(session, [int(p) for p in people if p.isdigit()])
    metrics = recognition.player_metrics(totals, links, mw.motm_wins(session), people)
    recognition.evaluate(session, metrics, now)
    pending = recognition.unannounced(session)
    if not pending:
        return 0
    names = _names(session)
    items = [(m.discord_id, names.get(m.discord_id, "A teammate"), m.label) for m in pending]
    try:
        discord_notify.post(config.MATCHDAY_CHANNEL_ID, embeds=[discord_notify.milestones_embed(items)],
                            content=" ".join(f"<@{i}>" for i in {i for i, _, _ in items}),
                            mention_ids=[i for i, _, _ in items])
    except discord_notify.DiscordApiError as exc:
        print(f"milestones: post failed: {exc}")
        return 0
    recognition.mark_announced(session, pending, now)
    return len(pending)


def player_of_the_month(session, now: datetime) -> int:
    """On or after the 1st, awards last month and posts it, once."""
    import recognition

    award = recognition.award_month(session, recognition.previous_month(now), _names(session), now)
    if award is None or award.announced_at:
        return 0
    ids = award.discord_ids.split(",")
    try:
        discord_notify.post(config.MATCHDAY_CHANNEL_ID, content=" ".join(f"<@{i}>" for i in ids),
                            embeds=[discord_notify.potm_embed(award.names, recognition.month_label(award.month),
                                                              award.votes)], mention_ids=ids)
    except discord_notify.DiscordApiError as exc:
        print(f"player of the month: post failed: {exc}")
        return 0
    award.announced_at = now
    session.commit()
    return 1


JOBS = [
    ("availability", availability_nudges),
    ("reminders", event_reminders),
    ("votes", open_votes),
    ("milestones", milestones),
    ("player_of_the_month", player_of_the_month),
]


def run(now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    results = {}
    with get_session() as session:
        for name, job in JOBS:
            try:
                results[name] = job(session, now)
            except Exception as exc:  # one broken job mustn't stop the others
                session.rollback()
                results[name] = f"failed: {exc}"
    return results


def main():
    if not config.NOTIFY_ENABLED:
        print("notifications not configured -- need DISCORD_BOT_TOKEN")
        return
    init_db()
    for name, result in run().items():
        print(f"{name}: {result}")


if __name__ == "__main__":
    main()
