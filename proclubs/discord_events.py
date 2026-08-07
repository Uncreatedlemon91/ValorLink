"""Read-only client for Discord's Guild Scheduled Events API.

REST only -- no gateway/websocket connection, since this app has no
always-on bot process. Changes are picked up by periodically polling (see
discord_events_poll.py), the same pattern as ea_client.py's data feeding
poll.py.

SHARED CREDENTIAL, BY EXPLICIT CHOICE: DISCORD_BOT_TOKEN is the same token
the main ValorLink bot uses, not a separate bot registered for this app.
That's a real deviation from this app's usual "share nothing" isolation
principle (see README.md) -- a compromise of this app's .env exposes the
real bot's full token, not just an OAuth client secret. Handle it, and this
module, accordingly.
"""
from __future__ import annotations

import httpx

import config

_API = "https://discord.com/api/v10"
_TIMEOUT = 15

# Discord's status enum for a guild scheduled event.
STATUS_SCHEDULED = 1
STATUS_ACTIVE = 2
STATUS_COMPLETED = 3
STATUS_CANCELED = 4


class DiscordApiError(Exception):
    pass


def list_scheduled_events() -> list[dict]:
    """Every scheduled event Discord currently has for DISCORD_GUILD_ID,
    unfiltered (includes completed/canceled ones -- see is_upcoming).

    Raises DiscordApiError on any failure. Callers must NOT treat that the
    same as "no events": silently returning [] on a transient failure would
    make services.sync_discord_events delete every previously-synced
    fixture, since it reads an empty list as "Discord canceled all of
    these." """
    try:
        resp = httpx.get(
            f"{_API}/guilds/{config.DISCORD_GUILD_ID}/scheduled-events",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise DiscordApiError(f"could not reach Discord's API: {exc}") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise DiscordApiError("Discord API returned a non-JSON response") from exc


def is_upcoming(discord_event: dict) -> bool:
    """False for events Discord has already marked completed or canceled --
    those shouldn't be (re)created as site fixtures."""
    return discord_event.get("status") not in (STATUS_COMPLETED, STATUS_CANCELED)
