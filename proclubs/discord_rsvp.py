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
# The position picker on an event that has a formation. The chosen slot key
# arrives in data.values[0] rather than in the custom_id, so this one only
# needs to carry the event: "rsvpslot:<event_id>".
SLOT_CUSTOM_ID_PREFIX = "rsvpslot"
# Discord rejects a select menu with no options at all, so a fully-picked
# squad drops the picker entirely rather than rendering an empty one.
MAX_SELECT_OPTIONS = 25

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


def parse_slot_custom_id(custom_id: str) -> int:
    """"rsvpslot:12" -> 12."""
    parts = (custom_id or "").split(":")
    if len(parts) != 2 or parts[0] != SLOT_CUSTOM_ID_PREFIX:
        raise InteractionError(f"not a position picker: {custom_id!r}")
    try:
        return int(parts[1])
    except ValueError as exc:
        raise InteractionError(f"malformed event id in {custom_id!r}") from exc


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


def build_embed(event, signups: list, roles: dict[int, str], site_url: str,
                slots: dict[str, str] | None = None) -> dict:
    """The announcement body: when, what, and who has answered so far.

    An event with a formation shows the team sheet -- every position and
    who has it, empty ones included -- because "what's still open" is the
    question people open the post to answer. Without a formation it falls
    back to the three plain columns.
    """
    slots = slots or {}
    lines = [f"**When:** {_discord_ts(event.scheduled_at)}"]
    if event.opponent:
        lines.append(f"**Opponent:** {event.opponent}")
    if event.description:
        lines.append("")
        lines.append(event.description)

    if slots:
        fields = _team_sheet_fields(event, signups, slots)
    else:
        fields = _flat_rsvp_fields(signups, roles)

    embed = {
        "title": f"{event.event_type}: {event.title}",
        "description": "\n".join(lines)[:4096],
        "fields": fields,
        "url": site_url,
    }
    if not event.signups_open:
        embed["footer"] = {"text": "Sign-ups are closed"}
    elif slots:
        embed["footer"] = {"text": "Pick a position below to sign up"}
    return embed


def _flat_rsvp_fields(signups: list, roles: dict[int, str]) -> list[dict]:
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
            "value": value[:1024],
            "inline": True,
        })
    return fields


def _team_sheet_fields(event, signups: list, slots: dict[str, str]) -> list[dict]:
    """The XI and bench as a team sheet, plus whoever answered without
    taking a shirt. Empty positions are listed too -- an absent line is
    invisible, and "who still needs covering" is the whole point."""
    by_slot = {s.slot_key: s for s in signups if s.slot_key}
    starting = [k for k in slots if not k.startswith("SUB")]
    bench = [k for k in slots if k.startswith("SUB")]

    def sheet(keys):
        return "\n".join(
            f"**{slots[k]}** — {by_slot[k].discord_name}" if k in by_slot
            else f"{slots[k]} — *open*"
            for k in keys
        ) or "—"

    filled = sum(1 for k in starting if k in by_slot)
    fields = [
        {"name": f"Starting XI ({filled}/{len(starting)})",
         "value": sheet(starting)[:1024], "inline": True},
    ]
    if any(k in by_slot for k in bench):
        fields.append({"name": "Bench",
                       "value": "\n".join(
                           f"**{slots[k]}** — {by_slot[k].discord_name}"
                           for k in bench if k in by_slot)[:1024],
                       "inline": True})

    # Answered but holding no position: still useful to staff (a "maybe" is
    # a body that might cover a gap), so never drop them off the post.
    others = [s for s in signups if not s.slot_key]
    if others:
        fields.append({
            "name": "No position yet",
            "value": "\n".join(
                f"{s.discord_name} — {SIGNUP_LABELS[s.status]}" for s in others)[:1024],
            "inline": False,
        })
    return fields


def build_components(event, signups: list | None = None,
                     slots: dict[str, str] | None = None) -> list[dict]:
    """The controls under the post.

    With a formation: a position picker listing every open shirt, plus
    Maybe / Can't make it. Picking a position IS signing up, so there's no
    separate "Going" button -- offering both would let someone be going
    with no position and think they'd picked one.

    Without a formation: the three plain buttons.

    Nothing at all once sign-ups close -- leaving dead controls on a closed
    event only invites presses that can't be honoured.
    """
    if not event.signups_open:
        return []

    slots = slots or {}
    if not slots:
        return [_button_row(event, SIGNUP_STATUSES)]

    taken = {s.slot_key for s in (signups or []) if s.slot_key}
    options = [
        {"label": label, "value": key,
         "description": "Bench" if key.startswith("SUB") else "Starting XI"}
        for key, label in slots.items() if key not in taken
    ][:MAX_SELECT_OPTIONS]

    rows = []
    if options:
        rows.append({
            "type": 1,
            "components": [{
                "type": 3,  # string select
                "custom_id": f"{SLOT_CUSTOM_ID_PREFIX}:{event.id}",
                "placeholder": "Pick your position",
                "options": options,
            }],
        })
    # Discord rejects a select with zero options, so a fully-picked squad
    # simply loses the picker -- the Maybe/Can't buttons stay, which is
    # exactly what's still meaningful at that point.
    rows.append(_button_row(event, ["maybe", "out"]))
    return rows


def _button_row(event, statuses: list[str]) -> dict:
    return {
        "type": 1,  # action row
        "components": [
            {
                "type": 2,  # button
                "style": _BUTTON_STYLES[status],
                "label": SIGNUP_LABELS[status],
                "emoji": {"name": _BUTTON_EMOJI[status]},
                "custom_id": f"{CUSTOM_ID_PREFIX}:{event.id}:{status}",
            }
            for status in statuses
        ],
    }


def announce(event, signups: list, roles: dict[int, str], site_url: str,
             slots: dict[str, str] | None = None) -> tuple[str, str]:
    """Posts the event to the configured channel. Returns (channel_id,
    message_id) for the caller to store, so later sign-ups can edit it."""
    channel_id = config.EVENTS_ANNOUNCE_CHANNEL_ID
    resp = discord_api.post(f"/channels/{channel_id}/messages", {
        "embeds": [build_embed(event, signups, roles, site_url, slots)],
        "components": build_components(event, signups, slots),
    })
    message_id = str(resp.json()["id"])
    return str(channel_id), message_id


def refresh(event, signups: list, roles: dict[int, str], site_url: str,
            slots: dict[str, str] | None = None) -> None:
    """Re-renders an already-posted announcement, so a sign-up made on the
    site shows up in Discord too. A no-op for an event that was never
    announced."""
    if not (event.discord_channel_id and event.discord_message_id):
        return
    discord_api.patch(
        f"/channels/{event.discord_channel_id}/messages/{event.discord_message_id}",
        {
            "embeds": [build_embed(event, signups, roles, site_url, slots)],
            "components": build_components(event, signups, slots),
        },
    )
