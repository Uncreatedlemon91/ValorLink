"""Shared low-level REST helper for Discord's API, used by discord_events.py
and discord_clips.py. Not a general-purpose Discord client -- just the
GET-with-429-retry plumbing both of those need, factored out once a second
module needed the exact same logic.

REST only -- no gateway/websocket connection, since this app has no
always-on bot process. Changes are picked up by periodically polling (see
discord_events_poll.py / discord_clips_poll.py), the same pattern as
ea_client.py's data feeding poll.py.

SHARED CREDENTIAL, BY EXPLICIT CHOICE: DISCORD_BOT_TOKEN is the same token
the main ValorLink bot uses, not a separate bot registered for this app.
That's a real deviation from this app's usual "share nothing" isolation
principle (see README.md) -- a compromise of this app's .env exposes the
real bot's full token, not just an OAuth client secret. Handle it, and
this module, accordingly.
"""
from __future__ import annotations

import time

import httpx

import config

_API = "https://discord.com/api/v10"
_TIMEOUT = 15

# A single bounded retry on 429 -- long enough to ride out the kind of
# sub-second-to-low-single-digit-second rate limit a low-volume route like
# these get, short enough not to hang a oneshot systemd run if Discord asks
# for longer. Sharing DISCORD_BOT_TOKEN with the always-on ValorLink bot
# means an occasional 429 here is expected contention, not a bug.
_MAX_RETRY_WAIT = 5.0


class DiscordApiError(Exception):
    pass


def _request(path: str, params: dict | None) -> httpx.Response:
    try:
        return httpx.get(
            f"{_API}{path}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            params=params, timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DiscordApiError(f"could not reach Discord's API: {exc}") from exc


def _discord_error_detail(resp: httpx.Response) -> str:
    """Discord's error responses carry a JSON body like {"message": "Missing
    Access", "code": 50001} -- httpx's own HTTPStatusError text is just the
    status line ("403 Forbidden"), which isn't enough to tell "bad token"
    apart from "bot isn't in that channel" apart from "channel doesn't
    exist". Appended to the raised DiscordApiError so that detail reaches
    wherever the error is surfaced (a flash message, a log line)."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    code = body.get("code") if isinstance(body, dict) else None
    if message and code is not None:
        return f" ({message}, code {code})"
    if message:
        return f" ({message})"
    return ""


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


def _request_post(path: str, json: dict) -> httpx.Response:
    try:
        return httpx.post(
            f"{_API}{path}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            json=json, timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DiscordApiError(f"could not reach Discord's API: {exc}") from exc


def post(path: str, json: dict) -> httpx.Response:
    """POST path (e.g. "/channels/123/messages") against Discord's API with
    the shared bot token, retrying once on a 429. Same failure semantics as
    get() -- see there."""
    resp = _request_post(path, json)

    if resp.status_code == 429:
        time.sleep(min(_MAX_RETRY_WAIT, _retry_after_seconds(resp)))
        resp = _request_post(path, json)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if resp.status_code == 429:
            raise DiscordApiError(
                "still rate-limited after retrying -- DISCORD_BOT_TOKEN is shared with the "
                "main ValorLink bot, so this can happen under contention"
            ) from exc
        raise DiscordApiError(f"could not reach Discord's API: {exc}{_discord_error_detail(resp)}") from exc

    return resp


def _request_patch(path: str, json: dict) -> httpx.Response:
    try:
        return httpx.patch(
            f"{_API}{path}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            json=json, timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DiscordApiError(f"could not reach Discord's API: {exc}") from exc


def patch(path: str, json: dict) -> httpx.Response:
    """PATCH path (e.g. "/channels/123/messages/456") with the shared bot
    token, retrying once on a 429. Same failure semantics as get(). Used to
    keep an event's RSVP announcement in step with sign-ups that happened
    on the site rather than through its buttons."""
    resp = _request_patch(path, json)

    if resp.status_code == 429:
        time.sleep(min(_MAX_RETRY_WAIT, _retry_after_seconds(resp)))
        resp = _request_patch(path, json)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if resp.status_code == 429:
            raise DiscordApiError(
                "still rate-limited after retrying -- DISCORD_BOT_TOKEN is shared with the "
                "main ValorLink bot, so this can happen under contention"
            ) from exc
        raise DiscordApiError(f"could not reach Discord's API: {exc}{_discord_error_detail(resp)}") from exc

    return resp


def get(path: str, params: dict | None = None) -> httpx.Response:
    """GET path (e.g. "/guilds/123/scheduled-events") against Discord's API
    with the shared bot token, retrying once on a 429. Returns the raw
    response with a 2xx status -- callers parse the body themselves. Raises
    DiscordApiError on a network failure or a non-2xx response (after the
    retry, for 429s)."""
    resp = _request(path, params)

    if resp.status_code == 429:
        time.sleep(min(_MAX_RETRY_WAIT, _retry_after_seconds(resp)))
        resp = _request(path, params)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if resp.status_code == 429:
            raise DiscordApiError(
                "still rate-limited after retrying -- DISCORD_BOT_TOKEN is shared with the "
                "main ValorLink bot, so this can happen under contention; the next scheduled "
                "poll will likely succeed"
            ) from exc
        raise DiscordApiError(f"could not reach Discord's API: {exc}{_discord_error_detail(resp)}") from exc

    return resp
