"""Platform-wide bans: a Discord identity banned here is blocked from
signing in — and has any live session revoked — anywhere on the platform,
since one Discord login already spans every unit.
"""
from __future__ import annotations

from tenancy.registry import PlatformBan, registry_session


class BanError(Exception):
    """Raised for a rejected ban action (already banned, not found)."""


def is_banned(discord_id: int) -> bool:
    with registry_session() as s:
        return s.query(PlatformBan).filter(PlatformBan.discord_id == discord_id).first() is not None


def ban_reason(discord_id: int) -> str | None:
    with registry_session() as s:
        ban = s.query(PlatformBan).filter(PlatformBan.discord_id == discord_id).one_or_none()
        return ban.reason if ban else None


def ban_user(discord_id: int, reason: str | None, actor_id: int | None) -> None:
    with registry_session() as s:
        existing = s.query(PlatformBan).filter(PlatformBan.discord_id == discord_id).one_or_none()
        if existing is not None:
            raise BanError("That user is already banned.")
        s.add(PlatformBan(
            discord_id=discord_id,
            reason=(reason or "").strip() or None,
            banned_by=actor_id,
        ))
        s.commit()


def unban_user(discord_id: int) -> None:
    with registry_session() as s:
        ban = s.query(PlatformBan).filter(PlatformBan.discord_id == discord_id).one_or_none()
        if ban is None:
            raise BanError("That user isn't banned.")
        s.delete(ban)
        s.commit()


def list_bans() -> list[dict]:
    with registry_session() as s:
        bans = s.query(PlatformBan).order_by(PlatformBan.banned_at.desc()).all()
        return [{
            "id": b.id, "discord_id": b.discord_id, "reason": b.reason,
            "banned_by": b.banned_by, "banned_at": b.banned_at,
        } for b in bans]
