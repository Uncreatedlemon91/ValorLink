"""What the bot says during the match week, and how it says it.

Two ways to reach people:

  channel posts  the team sheet, the vote and the match report, in the
                 fixture's own Discord thread when it has one, otherwise
                 the matchday channel (config.MATCHDAY_CHANNEL_ID).
  DMs            the personal ones: your shirt for Thursday, a reminder to
                 answer, a nudge to set your usual nights. Every DM links
                 back to the site, where anything with more than one
                 decision is done.

Nothing here decides *whether* to send -- matchweek.py and notify_poll.py
do that, and record each send so it happens once. A DM to somebody who
has DMs from server members turned off fails; that's counted, not raised,
so one closed inbox never stops the rest.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import discord_api
from matchweek import RATING_WORDS

DiscordApiError = discord_api.DiscordApiError

# custom_id prefixes for the post-match message's two pickers.
MOTM_PREFIX = "motm"
SELF_RATE_PREFIX = "selfrate"

_RED = 0xE31937
_BLUE = 0x79AEF7
_NAVY = 0x14233F


def _ts(when: datetime, style: str = "F") -> str:
    return f"<t:{int(when.replace(tzinfo=timezone.utc).timestamp())}:{style}>"


def _site(path: str) -> str:
    return f"{config.SITE_BASE_URL}{path}" if config.SITE_BASE_URL else path


def channel_for(event) -> str:
    """The fixture's thread if it was announced into one, else the matchday
    channel."""
    return str(event.discord_channel_id or config.MATCHDAY_CHANNEL_ID or "")


def post(channel_id: str, *, content: str = "", embeds: list | None = None,
         components: list | None = None, mention_ids: list[str] | None = None) -> str:
    """Posts to a channel and returns the message id. Mentions are listed
    explicitly, so text in a coach's note can never ping @everyone."""
    if not channel_id:
        raise DiscordApiError("no channel configured for matchday posts (MATCHDAY_CHANNEL_ID)")
    body = {"content": content[:2000], "embeds": embeds or [],
            "allowed_mentions": {"parse": [], "users": [str(i) for i in (mention_ids or [])][:100]}}
    if components:
        body["components"] = components
    resp = discord_api.post(f"/channels/{channel_id}/messages", body)
    return str(resp.json()["id"])


def send_dm(user_id: str, content: str, embeds: list | None = None) -> None:
    """Opens (or reuses) the DM channel with a member and posts to it."""
    channel = discord_api.post("/users/@me/channels", {"recipient_id": str(user_id)})
    discord_api.post(f"/channels/{channel.json()['id']}/messages", {
        "content": content[:2000], "embeds": embeds or [],
        "allowed_mentions": {"parse": []},
    })


def dm_many(user_ids: list[str], content_for) -> tuple[int, list[str]]:
    """DMs each person `content_for(user_id)`. Returns (sent, failed ids)."""
    if not config.NOTIFY_DMS:
        return 0, []
    sent, failed = 0, []
    for uid in user_ids:
        try:
            send_dm(uid, content_for(uid))
            sent += 1
        except DiscordApiError:
            failed.append(uid)
    return sent, failed


# --------------------------------------------------------------------------- #
# The team sheet
# --------------------------------------------------------------------------- #
def lineup_embed(event, xi: list[tuple[str, str, str]], bench: list[tuple[str, str, str]],
                 published_by: str | None) -> dict:
    """xi/bench: (slot label, discord id, name)."""
    def lines(rows):
        return "\n".join(f"`{label:<4}` <@{uid}>" for label, uid, _ in rows) or "—"
    opponent = f" vs {event.opponent}" if event.opponent else ""
    embed = {
        "title": f"Team sheet — {event.title}{opponent}",
        "url": _site(f"/events/{event.id}"),
        "color": _RED,
        "description": f"Kick-off {_ts(event.scheduled_at)}" + (f" · {event.formation}" if event.formation else ""),
        "fields": [{"name": "Starting XI", "value": lines(xi)[:1024], "inline": False}],
    }
    if bench:
        embed["fields"].append({"name": "Bench", "value": lines(bench)[:1024], "inline": False})
    if published_by:
        embed["footer"] = {"text": f"Picked by {published_by}"}
    return embed


def shirt_dm(event, label: str, starting: bool) -> str:
    where = f"starting at **{label}**" if starting else f"on the bench (**{label}**)"
    opponent = f" vs {event.opponent}" if event.opponent else ""
    return (f"You're {where} for **{event.title}{opponent}**, {_ts(event.scheduled_at)}.\n"
            f"Team sheet: {_site(f'/events/{event.id}')}")


# --------------------------------------------------------------------------- #
# Reminders
# --------------------------------------------------------------------------- #
def reminder_dm(event) -> str:
    opponent = f" vs {event.opponent}" if event.opponent else ""
    return (f"**{event.title}{opponent}** is {_ts(event.scheduled_at, 'R')} and we haven't "
            f"heard from you. Going, maybe or out? {_site(f'/events/{event.id}')}")


def availability_dm() -> str:
    return ("Which nights can you usually play? Set them once and the coaches can plan around "
            f"you — and mark any dates you're away: {_site('/availability')}")


# --------------------------------------------------------------------------- #
# After the match
# --------------------------------------------------------------------------- #
def vote_message(event, players: list[dict]) -> dict:
    """The post-match message: who to vote for, and a self-rating."""
    opponent = f" vs {event.opponent}" if event.opponent else ""
    embed = {
        "title": f"Full time — {event.title}{opponent}",
        "url": _site(f"/events/{event.id}/report"),
        "color": _NAVY,
        "description": ("Vote for your **Man of the Match** and rate your own game, out of ten. "
                        "Only players in the squad can vote, and not for themselves. Voting "
                        f"closes {_ts(event.vote_closes_at, 'R')}." if event.vote_closes_at else ""),
    }
    components = [
        {"type": 1, "components": [{
            "type": 3, "custom_id": f"{MOTM_PREFIX}:{event.id}",
            "placeholder": "Vote: Man of the Match",
            "options": [{"label": p["name"][:100], "value": p["id"]} for p in players[:25]],
        }]},
        {"type": 1, "components": [{
            "type": 3, "custom_id": f"{SELF_RATE_PREFIX}:{event.id}",
            "placeholder": "Rate your own game",
            "options": [{"label": f"{n} — {RATING_WORDS[n]}", "value": str(n)}
                        for n in range(10, 0, -1)],
        }]},
    ]
    return {"embeds": [embed], "components": components}


def parse_picker(custom_id: str) -> tuple[str, int]:
    """"motm:12" -> ("motm", 12). ValueError for anything else."""
    kind, _, rest = (custom_id or "").partition(":")
    if kind not in (MOTM_PREFIX, SELF_RATE_PREFIX) or not rest.isdigit():
        raise ValueError(custom_id)
    return kind, int(rest)


def report_embed(event, *, motm_names: list[str], motm_votes: int, top_rated: list[tuple[str, float]],
                 clips: list[str], night: dict | None = None) -> dict:
    opponent = f" vs {event.opponent}" if event.opponent else ""
    score = (f"**{event.us_score}–{event.opp_score}**"
             if event.us_score is not None and event.opp_score is not None else "")
    embed = {
        "title": f"Match report — {event.title}{opponent}",
        "url": _site(f"/events/{event.id}/report"),
        "color": _RED,
        "description": (score + "\n\n" if score else "") + (event.review_notes or "")[:3500],
        "fields": [],
    }
    if motm_names:
        embed["fields"].append({"name": "Man of the Match", "inline": True,
                                "value": f"{' & '.join(motm_names)} ({motm_votes} vote{'s' if motm_votes != 1 else ''})"})
    if night:
        embed["fields"].append({"name": "Match night", "inline": True,
                                "value": f"{night['record']} · GD {night['gd_text']}"})
    if top_rated:
        embed["fields"].append({"name": "Top rated (EA)", "inline": True,
                                "value": "\n".join(f"{n} · {r:.1f}" for n, r in top_rated)[:1024]})
    if clips:
        embed["fields"].append({"name": "Clips", "inline": False,
                                "value": "\n".join(clips)[:1024]})
    if event.review_published_by:
        embed["footer"] = {"text": f"Report by {event.review_published_by}"}
    return embed


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #
def milestones_embed(items: list[tuple[str, str, str]]) -> dict:
    """items: (discord id, name, milestone label)."""
    lines = [f"**{name}** — {label}" for _, name, label in items]
    return {"title": "Milestones", "color": _BLUE, "url": _site("/players"),
            "description": "\n".join(lines)[:4000]}


def potm_embed(names: str, month: str, votes: int) -> dict:
    return {"title": f"Player of the Month — {month}", "color": _RED, "url": _site("/players"),
            "description": (f"**{names}**, with {votes} Man of the Match vote{'s' if votes != 1 else ''} "
                            f"from the squad this month.")}



def goal_dm(text: str, coach: str | None) -> str:
    return (f"{coach or 'Your coach'} set you a new goal: **{text}**\n"
            f"Track your progress on your player file: {_site('/players/me')}")
