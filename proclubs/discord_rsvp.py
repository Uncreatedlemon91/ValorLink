"""The Discord half of event sign-ups: the announcement, its buttons, and
verifying the button presses that come back.

Unlike every other Discord integration in this app, this one is
two-directional and NOT polled. Discord delivers an interaction (a button
press) to a URL we configure -- an ordinary signed HTTPS POST to
/discord/interactions -- so a press lands instantly with no gateway
connection and no always-on bot process, which keeps this app's "web app
plus systemd timers" shape intact.

Trust model: an interaction request is only trustworthy because of its
Ed25519 signature. Discord signs every request with the application's key
and expects unsigned or badly-signed requests to be rejected with a 401 --
it actively probes the endpoint with deliberately-invalid signatures when
you save the URL, and refuses to accept the endpoint unless those are
rejected. verify_signature() below is therefore load-bearing security, not
a formality: without it anyone who learns the URL could sign up, or
un-sign-up, anyone they like.
"""
from __future__ import annotations

from datetime import datetime, timezone

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

import config
import discord_api
from models import SIGNUP_LABELS, SIGNUP_STATUSES

DiscordApiError = discord_api.DiscordApiError

# Discord's interaction type enum (the subset this endpoint handles).
INTERACTION_PING = 1
INTERACTION_MESSAGE_COMPONENT = 3

# Discord's interaction *response* type enum.
RESPONSE_PONG = 1
RESPONSE_UPDATE_MESSAGE = 7          # edit the message the button lives on

# custom_id format for the sign-up buttons: "rsvp:<event_id>:<status>".
# Discord echoes custom_id back verbatim on press, so it's how a press is
# tied to an event without any server-side state between the two requests.
CUSTOM_ID_PREFIX = "rsvp"

_BUTTON_STYLES = {"going": 3, "maybe": 2, "out": 4}  # green, grey, red
_BUTTON_EMOJI = {"going": "✅", "maybe": "❔", "out": "❌"}


class InteractionError(Exception):
    """Raised for a request that is well-formed but not something this
    endpoint handles, or that names an event that no longer exists."""


def verify_signature(*, signature: str, timestamp: str, body: bytes) -> bool:
    """True if `body` really was signed by our Discord application.

    Discord signs (timestamp + body) with Ed25519. Any failure at all --
    missing header, non-hex signature, wrong length, bad signature -- is a
    rejection rather than an exception, because the caller's only correct
    response to every one of them is an identical 401.
    """
    if not config.DISCORD_PUBLIC_KEY or not signature or not timestamp:
        return False
    try:
        verify_key = VerifyKey(bytes.fromhex(config.DISCORD_PUBLIC_KEY))
        verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature))
    except (BadSignatureError, ValueError, TypeError):
        return False
    return True


def parse_custom_id(custom_id: str) -> tuple[int, str]:
    """"rsvp:12:going" -> (12, "going"). Raises InteractionError for
    anything else -- including a button from some other feature, which is
    not an error worth logging as a failure but is not ours to handle."""
    parts = (custom_id or "").split(":")
    if len(parts) != 3 or parts[0] != CUSTOM_ID_PREFIX:
        raise InteractionError(f"not an event sign-up button: {custom_id!r}")
    try:
        event_id = int(parts[1])
    except ValueError as exc:
        raise InteractionError(f"malformed event id in {custom_id!r}") from exc
    if parts[2] not in SIGNUP_STATUSES:
        raise InteractionError(f"unknown sign-up status in {custom_id!r}")
    return event_id, parts[2]


def interaction_user(interaction: dict) -> dict:
    """The presser's identity, from wherever Discord put it.

    In a guild the user is nested under "member"; in a DM it's top-level
    "user". Buttons on a guild announcement always take the first path, but
    handling both costs one line and avoids a KeyError crash if a message
    ever gets forwarded somewhere unexpected.
    """
    user = (interaction.get("member") or {}).get("user") or interaction.get("user")
    if not user or not user.get("id"):
        raise InteractionError("interaction carried no user")
    return {
        "id": int(user["id"]),
        # global_name is Discord's current display name; username is the
        # legacy handle and is always present as a fallback.
        "name": user.get("global_name") or user.get("username") or f"User {user['id']}",
        "avatar": user.get("avatar"),
    }


def _discord_ts(when: datetime) -> str:
    """Discord renders <t:epoch:F> in each reader's own timezone, which
    beats writing a UTC string that everyone has to convert in their head.
    Event times are stored naive-UTC (see services), so attach UTC before
    taking the timestamp rather than letting Python assume local time."""
    return f"<t:{int(when.replace(tzinfo=timezone.utc).timestamp())}:F>"


def build_embed(event, signups: list, roles: dict[int, str], site_url: str) -> dict:
    """The announcement body: when, what, and who has answered so far,
    split into the three columns people actually scan for."""
    lines = [f"**When:** {_discord_ts(event.scheduled_at)}"]
    if event.opponent:
        lines.append(f"**Opponent:** {event.opponent}")
    if event.description:
        lines.append("")
        lines.append(event.description)

    fields = []
    for status in SIGNUP_STATUSES:
        named = [s for s in signups if s.status == status]
        # The Tactics position is what makes this roster useful at a glance
        # -- "we have four defenders and no keeper" is invisible in a plain
        # list of names.
        value = "\n".join(
            f"{s.discord_name}" + (f" — {roles[s.discord_user_id]}" if s.discord_user_id in roles else "")
            for s in named
        ) or "—"
        fields.append({
            "name": f"{SIGNUP_LABELS[status]} ({len(named)})",
            "value": value[:1024],  # Discord's per-field cap
            "inline": True,
        })

    embed = {
        "title": f"{event.event_type}: {event.title}",
        "description": "\n".join(lines)[:4096],
        "fields": fields,
        "url": site_url,
    }
    if not event.signups_open:
        embed["footer"] = {"text": "Sign-ups are closed"}
    return embed


def build_components(event) -> list[dict]:
    """The three sign-up buttons, or none at all once sign-ups close --
    leaving dead buttons on a closed event would invite presses that can
    only ever fail."""
    if not event.signups_open:
        return []
    return [{
        "type": 1,  # action row
        "components": [
            {
                "type": 2,  # button
                "style": _BUTTON_STYLES[status],
                "label": SIGNUP_LABELS[status],
                "emoji": {"name": _BUTTON_EMOJI[status]},
                "custom_id": f"{CUSTOM_ID_PREFIX}:{event.id}:{status}",
            }
            for status in SIGNUP_STATUSES
        ],
    }]


def announce(event, signups: list, roles: dict[int, str], site_url: str) -> tuple[str, str]:
    """Posts the event to the configured channel. Returns (channel_id,
    message_id) for the caller to store, so later sign-ups can edit it."""
    channel_id = config.EVENTS_ANNOUNCE_CHANNEL_ID
    resp = discord_api.post(f"/channels/{channel_id}/messages", {
        "embeds": [build_embed(event, signups, roles, site_url)],
        "components": build_components(event),
    })
    message_id = str(resp.json()["id"])
    return str(channel_id), message_id


def refresh(event, signups: list, roles: dict[int, str], site_url: str) -> None:
    """Re-renders an already-posted announcement, so a sign-up made on the
    site shows up in Discord too. A no-op for an event that was never
    announced."""
    if not (event.discord_channel_id and event.discord_message_id):
        return
    discord_api.patch(
        f"/channels/{event.discord_channel_id}/messages/{event.discord_message_id}",
        {
            "embeds": [build_embed(event, signups, roles, site_url)],
            "components": build_components(event),
        },
    )
