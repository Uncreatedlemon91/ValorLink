"""Live role/channel lists for a unit's Discord server, fetched with the
bot's own token so Command Tent can offer dropdowns instead of asking
officers to hand-copy IDs out of Discord's Developer Mode.

The web process and the bot share the same DISCORD_BOT_TOKEN (both systemd
units load the same .env in production -- see deploy/README.md), so this
calls Discord's REST API directly rather than going through the bot process.

Best-effort throughout: any picker that can't be filled in (no bot token
configured, the bot hasn't been invited to the guild yet, Discord is
unreachable) returns None, and callers fall back to a plain ID text box
instead of blocking the page.
"""
from __future__ import annotations

import os
import time

import httpx

_DISCORD_API = "https://discord.com/api"
# Short enough that a role/channel created moments ago shows up quickly;
# long enough that reopening Command Tent doesn't re-fetch on every click.
_CACHE_TTL = 60
_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}

# Discord channel types that make sense as a log/announcement/roster target.
_TEXTISH_CHANNEL_TYPES = {0, 5}  # GUILD_TEXT, GUILD_ANNOUNCEMENT


def _bot_token() -> str:
    return os.getenv("DISCORD_BOT_TOKEN", "")


def _fetch(kind: str, guild_id: int) -> list[dict] | None:
    key = (kind, guild_id)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        with httpx.Client(timeout=5) as client:
            r = client.get(
                f"{_DISCORD_API}/guilds/{guild_id}/{kind}",
                headers={"Authorization": f"Bot {_bot_token()}"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        # Serve a still-cached value through a transient failure; otherwise
        # let the caller fall back to the plain text box for this request.
        return cached[1] if cached else None
    _cache[key] = (now, data)
    return data


def guild_roles(guild_id: int | None) -> list[dict] | None:
    """This guild's roles, highest first, excluding @everyone -- or None if
    they can't be fetched right now."""
    if not guild_id or not _bot_token():
        return None
    roles = _fetch("roles", guild_id)
    if roles is None:
        return None
    everyone_id = str(guild_id)
    filtered = sorted(
        (r for r in roles if r["id"] != everyone_id),
        key=lambda r: r["position"], reverse=True,
    )
    return [{"id": int(r["id"]), "name": r["name"]} for r in filtered]


def guild_channels(guild_id: int | None) -> list[dict] | None:
    """This guild's text/announcement channels, in server order -- or None
    if they can't be fetched right now."""
    if not guild_id or not _bot_token():
        return None
    channels = _fetch("channels", guild_id)
    if channels is None:
        return None
    filtered = sorted(
        (c for c in channels if c.get("type") in _TEXTISH_CHANNEL_TYPES),
        key=lambda c: c.get("position", 0),
    )
    return [{"id": int(c["id"]), "name": c["name"]} for c in filtered]
