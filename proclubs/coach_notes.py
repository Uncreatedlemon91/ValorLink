"""Coach notes in Discord: each note staff write on a player's file is also
posted to the staff channel (config.COACH_NOTES_CHANNEL_ID, which defaults
to the recruitment channel), the same way trial notes are (see
recruitment.post_feedback). Deleting the note on the site deletes the
Discord copy too.

The channel must be staff-only: a coach note is the one thing in a player's
file the player never sees. Nobody is pinged -- the player's mention renders
as a name, and allowed_mentions is empty so a note can't reach @everyone.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
import discord_api
from models import CoachNote
from services import get_player, live_contract_for

_BLUE = 0x79AEF7


def _site(path: str) -> str:
    return f"{config.SITE_BASE_URL}{path}" if config.SITE_BASE_URL else path


def note_embed(session: Session, note: CoachNote) -> dict:
    player = get_player(session, note.discord_id)
    name = player.display_name if player else note.discord_id
    contract = live_contract_for(session, note.discord_id)
    fields = [{"name": "Discord", "value": f"<@{note.discord_id}>", "inline": True}]
    if contract is not None:
        positions = " / ".join(p for p in (contract.position, contract.secondary_position) if p)
        fields.append({"name": "Squad", "value": " · ".join(
            x for x in (contract.squad_status, positions) if x), "inline": True})
    if player is not None and player.club_role:
        fields.append({"name": "Club role", "value": player.club_role, "inline": True})
    return {
        "title": f"Coach note — {name}"[:256],
        "url": _site(f"/players/{note.discord_id}#coach-notes"),
        "color": _BLUE,
        "description": note.body[:4000],
        "fields": fields,
        "footer": {"text": f"From {note.author_name} · staff only"},
        "timestamp": (note.created_at or datetime.utcnow()).replace(tzinfo=timezone.utc).isoformat(),
    }


def post(session: Session, note: CoachNote) -> str | None:
    """Posts a coach note to the staff channel. Returns why it didn't go,
    or None. Never raises: the note is saved either way."""
    if not config.COACH_NOTES_DISCORD_ENABLED:
        return None
    try:
        resp = discord_api.post(f"/channels/{config.COACH_NOTES_CHANNEL_ID}/messages",
                                {"embeds": [note_embed(session, note)], "allowed_mentions": {"parse": []}})
    except discord_api.DiscordApiError as exc:
        return str(exc)
    note.discord_message_id = str(resp.json().get("id") or "") or None
    note.discord_channel_id = str(config.COACH_NOTES_CHANNEL_ID)
    session.commit()
    return None


def unpost(note: CoachNote) -> str | None:
    """Deletes the Discord copy of a note being deleted. Returns why it
    couldn't, or None. A copy that's already gone counts as done."""
    if not (note.discord_message_id and note.discord_channel_id):
        return None
    try:
        discord_api.delete(f"/channels/{note.discord_channel_id}/messages/{note.discord_message_id}")
    except discord_api.DiscordApiError as exc:
        if "404" in str(exc):
            return None
        return str(exc)
    return None
