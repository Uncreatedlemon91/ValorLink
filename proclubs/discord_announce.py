"""Posts a rich embed to a configured Discord channel when an article goes
live on the site, linking back to it -- see config.NEWS_ANNOUNCE_CHANNEL_ID
and app.py's news_new/news_edit routes.

The announcement itself is one-directional and synchronous (site ->
Discord, sent the moment an article is published), unlike the events/clips
sync in discord_events.py/discord_clips.py -- this app is the source of
truth for articles, there's no "did Discord change" to notice about the
post itself. Reactions on that message are the one thing that DOES need
polling afterward, since people react on their own time -- see
fetch_reaction_count() and discord_reactions_poll.py.

Reuses DISCORD_BOT_TOKEN, same sharing tradeoff as those two modules -- see
proclubs/README.md.
"""
from __future__ import annotations

from datetime import datetime

import discord_api

DiscordApiError = discord_api.DiscordApiError

_CATEGORY_COLOR = {
    "News": 0x4C7CE0,
    "Transfer": 0x4CAF7D,
    "Match Highlight": 0xE0A64C,
}
_DEFAULT_COLOR = 0x5865F2  # Discord "blurple" -- sane fallback for an unrecognized category


def build_embed(*, title: str, url: str, summary: str | None, category: str,
                 author_name: str | None, cover_image_url: str | None,
                 published_at: datetime) -> dict:
    """Pure -- no network. Split out from announce() so the embed shape can
    be tested without mocking Discord."""
    embed: dict = {
        "title": title,
        "url": url,
        "color": _CATEGORY_COLOR.get(category, _DEFAULT_COLOR),
        "author": {"name": category},
        "timestamp": published_at.isoformat(),
    }
    if summary:
        embed["description"] = summary
    if author_name:
        embed["footer"] = {"text": f"Posted by {author_name}"}
    if cover_image_url:
        embed["image"] = {"url": cover_image_url}
    return embed


def announce(channel_id: str, embed: dict) -> str:
    """Posts the embed to channel_id, returning the new message's id (so
    the caller can save it -- see Article.discord_message_id -- and later
    check reactions on it). Raises DiscordApiError on failure -- callers
    must decide whether that should be surfaced or swallowed (see app.py:
    a Discord hiccup never blocks publishing, it's flashed to staff
    instead)."""
    resp = discord_api.post(f"/channels/{channel_id}/messages", json={"embeds": [embed]})
    return resp.json()["id"]


def fetch_reaction_count(channel_id: str, message_id: str) -> int:
    """Total reactions on a message, every emoji summed together -- not
    just one specific emoji, so it doesn't matter whether someone reacted
    with a heart, a fire, or anything else. Raises DiscordApiError on
    failure (e.g. the message was deleted) -- see discord_reactions_poll.py
    for how that's handled per-article rather than aborting the whole run."""
    resp = discord_api.get(f"/channels/{channel_id}/messages/{message_id}")
    reactions = resp.json().get("reactions") or []
    return sum(r.get("count", 0) for r in reactions)
