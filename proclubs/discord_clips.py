"""Read-only client for a Discord channel's recent messages, used to pull
video clips staff post there onto the site's Clips page.

Scope, deliberately: only video *files* uploaded directly to Discord
(content_type starting "video/") are treated as clips. A pasted YouTube/
Twitch/Streamable link shows up in Discord as a rich embed, not a file
attachment, and isn't picked up here -- the URL varies by provider, and
reliably turning an arbitrary link into an embeddable player is a bigger
job than this first pass covers.

Networking (bot-token auth, 429 retry) lives in discord_api.py, shared with
discord_events.py.
"""
from __future__ import annotations

import config
import discord_api

_VIDEO_CONTENT_PREFIX = "video/"

DiscordApiError = discord_api.DiscordApiError


def list_recent_messages(channel_id: str, limit: int = 50) -> list[dict]:
    """The channel's most recent `limit` messages, newest first (Discord's
    default order). Raises DiscordApiError on failure -- same reasoning as
    discord_events.list_scheduled_events: a caller must not treat that as
    "no messages," or a transient failure could look like every clip was
    deleted."""
    resp = discord_api.get(f"/channels/{channel_id}/messages", params={"limit": limit})
    try:
        return resp.json()
    except ValueError as exc:
        raise DiscordApiError("Discord API returned a non-JSON response") from exc


def video_attachments(message: dict) -> list[dict]:
    """Every attachment on this message that's a playable video file."""
    return [
        a for a in message.get("attachments", [])
        if (a.get("content_type") or "").startswith(_VIDEO_CONTENT_PREFIX)
    ]


def jump_url(channel_id: str, message_id: str) -> str:
    """A permanent link to the message in Discord -- doesn't expire the way
    attachment CDN URLs do, used as the fallback once a clip's video URL is
    too old to still work (see services.sync_clips)."""
    return f"https://discord.com/channels/{config.DISCORD_GUILD_ID}/{channel_id}/{message_id}"
