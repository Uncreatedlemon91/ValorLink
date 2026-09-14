"""Twitch Helix client: "is this channel live right now," nothing more.

Uses the client-credentials grant (an app access token, not a user token --
we're only ever reading public stream state, never acting as a Twitch user).
The token is cached in memory until shortly before it expires; stream
lookups are cached too, so a page of streamer cards doesn't fan out into
one Twitch call per card, and a cache that's gone stale refreshes behind
the request rather than inside it (see cache.py).
"""
from __future__ import annotations

import time

import httpx

import cache
import config

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_STREAMS_URL = "https://api.twitch.tv/helix/streams"
_TIMEOUT = 10

# Live status wants to be current, but never at the cost of a visitor
# waiting on Twitch mid-render: past the fresh window the last known answer
# is served straight away and refreshed on a background thread (see
# cache.py). max_stale is the point where "who's live" is too old to show at
# all, so a caller waits for a real lookup instead.
_token_cache: dict = {"value": None, "expires_at": 0.0}
_streams_cache = cache.SwrCache(fresh_for=30, max_stale=600)


class TwitchApiError(Exception):
    pass


def _app_token() -> str:
    now = time.monotonic()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": config.TWITCH_CLIENT_ID,
                "client_secret": config.TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise TwitchApiError(str(exc)) from exc

    _token_cache["value"] = data["access_token"]
    # Refresh a couple minutes early rather than racing the real expiry.
    _token_cache["expires_at"] = now + max(60, int(data.get("expires_in", 3600)) - 120)
    return _token_cache["value"]


def live_streams(logins: list[str]) -> dict[str, dict]:
    """login (lowercase) -> stream info, for whichever of ``logins`` are
    currently live AND playing config.TWITCH_GAME_FILTER (case-insensitive;
    an empty filter disables this and counts any live stream). A roster
    member streaming some other game is simply absent from the result, same
    as if they weren't live at all -- this showcase is "who's playing our
    game right now," not "who's live at all." Returns {} if Twitch isn't
    configured or the lookup fails -- "can't tell who's live" degrades to
    "show nobody as live," never an error page."""
    if not config.TWITCH_ENABLED or not logins:
        return {}

    key = tuple(sorted(login.lower() for login in logins))
    return _streams_cache.get(key, lambda: _fetch_streams(key))


def _fetch_streams(key: tuple[str, ...]) -> dict[str, dict]:
    try:
        token = _app_token()
        resp = httpx.get(
            _STREAMS_URL,
            params=[("user_login", login) for login in key],
            headers={
                "Client-Id": config.TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {token}",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        game_filter = config.TWITCH_GAME_FILTER.strip().lower()
        result = {}
        for stream in resp.json().get("data", []):
            if game_filter and stream.get("game_name", "").strip().lower() != game_filter:
                continue
            login = stream.get("user_login", "").lower()
            result[login] = {
                "title": stream.get("title", ""),
                "game_name": stream.get("game_name", ""),
                "viewer_count": stream.get("viewer_count", 0),
                "thumbnail_url": (stream.get("thumbnail_url") or "")
                    .replace("{width}", "440").replace("{height}", "248"),
                "started_at": stream.get("started_at"),
                "url": f"https://twitch.tv/{stream.get('user_login', login)}",
            }
    except (httpx.HTTPError, TwitchApiError, KeyError):
        return {}

    return result
