"""Posts a rich embed to a configured Discord channel when an article goes
live on the site, linking back to it -- see config.NEWS_ANNOUNCE_CHANNEL_ID
and app.py's news_new/news_edit routes.

One-directional (site -> Discord) and synchronous, unlike the events/clips
sync in discord_events.py/discord_clips.py: this app is the source of truth
for articles, so there's no "did Discord change" to notice later on a poll
-- the message just goes out the moment an article is published.

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


def announce(channel_id: str, embed: dict) -> None:
    """Posts the embed to channel_id. Raises DiscordApiError on failure --
    callers must decide whether that should be surfaced or swallowed (see
    app.py: a Discord hiccup never blocks publishing, it's flashed to staff
    instead)."""
    discord_api.post(f"/channels/{channel_id}/messages", json={"embeds": [embed]})
