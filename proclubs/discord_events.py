"""Read-only client for Discord's Guild Scheduled Events API.

Networking (bot-token auth, 429 retry) lives in discord_api.py, shared with
discord_clips.py -- this module just knows the scheduled-events endpoint
shape and how to interpret it.
"""
from __future__ import annotations

import discord_api
import config

_CDN = "https://cdn.discordapp.com"

# Discord's status enum for a guild scheduled event.
STATUS_SCHEDULED = 1
STATUS_ACTIVE = 2
STATUS_COMPLETED = 3
STATUS_CANCELED = 4

DiscordApiError = discord_api.DiscordApiError


def list_scheduled_events() -> list[dict]:
    """Every scheduled event Discord currently has for DISCORD_GUILD_ID,
    unfiltered (includes completed/canceled ones -- see is_upcoming).

    Raises DiscordApiError on any failure. Callers must NOT treat that the
    same as "no events": silently returning [] on a transient failure would
    make services.sync_discord_events delete every previously-synced
    fixture, since it reads an empty list as "Discord canceled all of
    these." """
    resp = discord_api.get(f"/guilds/{config.DISCORD_GUILD_ID}/scheduled-events")
    try:
        return resp.json()
    except ValueError as exc:
        raise DiscordApiError("Discord API returned a non-JSON response") from exc


def is_upcoming(discord_event: dict) -> bool:
    """False for events Discord has already marked completed or canceled --
    those shouldn't be (re)created as site fixtures."""
    return discord_event.get("status") not in (STATUS_COMPLETED, STATUS_CANCELED)


def cover_image_url(discord_event: dict) -> str | None:
    """The event's cover photo, if the organizer set one when creating it in
    Discord. Discord's API only gives back an image hash, not a full URL --
    same idea as a user avatar hash -- so build the actual CDN URL from it.
    None if the event has no cover image."""
    image_hash = discord_event.get("image")
    if not image_hash:
        return None
    return f"{_CDN}/guild-events/{discord_event['id']}/{image_hash}.png"
