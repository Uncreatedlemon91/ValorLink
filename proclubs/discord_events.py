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

import time

import httpx

import config

_API = "https://discord.com/api/v10"
_TIMEOUT = 15

# A single bounded retry on 429 -- long enough to ride out the kind of
# sub-second-to-low-single-digit-second rate limit a low-volume route like
# this one gets, short enough not to hang a oneshot systemd run if Discord
# asks for longer. Sharing DISCORD_BOT_TOKEN with the always-on ValorLink
# bot means an occasional 429 here is expected contention, not a bug -- see
# the module docstring.
_MAX_RETRY_WAIT = 5.0

# Discord's status enum for a guild scheduled event.
STATUS_SCHEDULED = 1
STATUS_ACTIVE = 2
STATUS_COMPLETED = 3
STATUS_CANCELED = 4


class DiscordApiError(Exception):
    pass


def _get(url: str) -> httpx.Response:
    try:
        return httpx.get(
            url, headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}, timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DiscordApiError(f"could not reach Discord's API: {exc}") from exc


def _retry_after_seconds(resp: httpx.Response, default: float = 1.0) -> float:
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        return float(resp.json().get("retry_after", default))
    except (ValueError, TypeError, KeyError):
        return default


def list_scheduled_events() -> list[dict]:
    """Every scheduled event Discord currently has for DISCORD_GUILD_ID,
    unfiltered (includes completed/canceled ones -- see is_upcoming).

    Raises DiscordApiError on any failure. Callers must NOT treat that the
    same as "no events": silently returning [] on a transient failure would
    make services.sync_discord_events delete every previously-synced
    fixture, since it reads an empty list as "Discord canceled all of
    these." """
    url = f"{_API}/guilds/{config.DISCORD_GUILD_ID}/scheduled-events"
    resp = _get(url)

    if resp.status_code == 429:
        time.sleep(min(_MAX_RETRY_WAIT, _retry_after_seconds(resp)))
        resp = _get(url)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if resp.status_code == 429:
            raise DiscordApiError(
                "still rate-limited after retrying -- DISCORD_BOT_TOKEN is shared with the "
                "main ValorLink bot, so this can happen under contention; the next scheduled "
                "poll will likely succeed"
            ) from exc
        raise DiscordApiError(f"could not reach Discord's API: {exc}") from exc

    try:
        return resp.json()
    except ValueError as exc:
        raise DiscordApiError("Discord API returned a non-JSON response") from exc


def is_upcoming(discord_event: dict) -> bool:
    """False for events Discord has already marked completed or canceled --
    those shouldn't be (re)created as site fixtures."""
    return discord_event.get("status") not in (STATUS_COMPLETED, STATUS_CANCELED)
