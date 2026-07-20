"""Alliances — coalitions of units, with a mutual-consent handshake.

A unit's admin creates an alliance or invites another unit; that unit only
becomes a member once *its own* admin accepts. All membership lives in the
registry (control-plane), keyed by unit slug, so an alliance can span units
without touching any unit's private database.

Every mutating call is expressed in terms of the *acting unit* — the unit whose
Command Tent the admin is in — so a unit can only ever change its own
memberships, never another unit's.
"""
from __future__ import annotations

import re

from tenancy.registry import (
    Alliance,
    AllianceMember,
    Tenant,
    registry_session,
)

_SLUG_RE = re.compile(r"^[a-z0-9-]{2,30}$")


class AllianceError(Exception):
    """A user-facing validation failure."""


def _names_for(session, slugs: set[str]) -> dict[str, str]:
    if not slugs:
        return {}
    rows = session.query(Tenant.slug, Tenant.name).filter(Tenant.slug.in_(slugs)).all()
    return {slug: name for slug, name in rows}


def create_alliance(name: str, slug: str, founder_slug: str) -> str:
    """Create an alliance with the acting unit as its founding member."""
    name = name.strip()
    slug = slug.strip().lower()
    if not name:
        raise AllianceError("An alliance needs a name.")
    if not _SLUG_RE.match(slug):
        raise AllianceError("The handle must be 2–30 lowercase letters, numbers, or hyphens.")
    with registry_session() as s:
        if s.query(Alliance).filter(Alliance.slug == slug).first():
            raise AllianceError(f"The handle “{slug}” is already taken.")
        alliance = Alliance(slug=slug, name=name)
        s.add(alliance)
        s.flush()
        s.add(AllianceMember(alliance_id=alliance.id, unit_slug=founder_slug,
                             role="founder", status="active"))
        s.commit()
        return f"Alliance “{name}” raised. Invite units to join it."


def invite_unit(alliance_id: int, target_slug: str, inviter_slug: str) -> str:
    """Invite another unit. The acting unit must already be an active member."""
    target_slug = target_slug.strip().lower()
    with registry_session() as s:
        alliance = s.get(Alliance, alliance_id)
        if alliance is None:
            raise AllianceError("That alliance no longer exists.")
        actor = _membership(s, alliance_id, inviter_slug)
        if actor is None or actor.status != "active":
            raise AllianceError("Only a member unit can invite others.")
        if target_slug == inviter_slug:
            raise AllianceError("That's your own unit.")
        if s.query(Tenant).filter(Tenant.slug == target_slug).first() is None:
            raise AllianceError(f"No unit with the handle “{target_slug}”.")
        existing = _membership(s, alliance_id, target_slug)
        if existing is not None:
            raise AllianceError(
                "That unit is already a member." if existing.status == "active"
                else "That unit has already been invited.")
        s.add(AllianceMember(alliance_id=alliance_id, unit_slug=target_slug,
                             role="member", status="invited", invited_by_slug=inviter_slug))
        s.commit()
        return f"Invitation sent to {target_slug}."


def accept_invite(alliance_id: int, unit_slug: str) -> str:
    with registry_session() as s:
        m = _membership(s, alliance_id, unit_slug)
        if m is None or m.status != "invited":
            raise AllianceError("There's no pending invitation to accept.")
        m.status = "active"
        s.commit()
        alliance = s.get(Alliance, alliance_id)
        return f"Joined {alliance.name}." if alliance else "Invitation accepted."


def leave_alliance(alliance_id: int, unit_slug: str) -> str:
    """Decline an invite or leave an alliance. If no active members remain, the
    alliance is dissolved."""
    with registry_session() as s:
        m = _membership(s, alliance_id, unit_slug)
        if m is None:
            raise AllianceError("Your unit isn't part of that alliance.")
        s.delete(m)
        s.flush()
        remaining = (
            s.query(AllianceMember)
            .filter(AllianceMember.alliance_id == alliance_id,
                    AllianceMember.status == "active")
            .count()
        )
        if remaining == 0:
            # Dissolve the empty alliance (and any lingering invitations).
            s.query(AllianceMember).filter(AllianceMember.alliance_id == alliance_id).delete()
            alliance = s.get(Alliance, alliance_id)
            if alliance is not None:
                s.delete(alliance)
        s.commit()
        return "Left the alliance."


def _membership(session, alliance_id: int, unit_slug: str):
    return (
        session.query(AllianceMember)
        .filter(AllianceMember.alliance_id == alliance_id,
                AllianceMember.unit_slug == unit_slug)
        .one_or_none()
    )


def alliances_for_unit(unit_slug: str) -> list[dict]:
    """Active alliances the unit belongs to, each with its fellow members."""
    with registry_session() as s:
        memberships = (
            s.query(AllianceMember)
            .filter(AllianceMember.unit_slug == unit_slug,
                    AllianceMember.status == "active")
            .all()
        )
        out = []
        for m in memberships:
            alliance = s.get(Alliance, m.alliance_id)
            if alliance is None:
                continue
            members = _active_members(s, alliance.id)
            out.append({
                "id": alliance.id, "name": alliance.name, "slug": alliance.slug,
                "role": m.role, "members": members, "count": len(members),
            })
        out.sort(key=lambda a: a["name"].lower())
        return out


def pending_invites_for_unit(unit_slug: str) -> list[dict]:
    with registry_session() as s:
        invites = (
            s.query(AllianceMember)
            .filter(AllianceMember.unit_slug == unit_slug,
                    AllianceMember.status == "invited")
            .all()
        )
        out = []
        for m in invites:
            alliance = s.get(Alliance, m.alliance_id)
            if alliance is None:
                continue
            names = _names_for(s, {m.invited_by_slug} if m.invited_by_slug else set())
            out.append({
                "id": alliance.id, "name": alliance.name, "slug": alliance.slug,
                "invited_by": names.get(m.invited_by_slug, m.invited_by_slug or "a member"),
            })
        return out


def _active_members(session, alliance_id: int) -> list[dict]:
    rows = (
        session.query(AllianceMember)
        .filter(AllianceMember.alliance_id == alliance_id,
                AllianceMember.status == "active")
        .all()
    )
    names = _names_for(session, {r.unit_slug for r in rows})
    members = [{"slug": r.unit_slug, "name": names.get(r.unit_slug, r.unit_slug),
                "role": r.role} for r in rows]
    members.sort(key=lambda m: (m["role"] != "founder", m["name"].lower()))
    return members


def alliance_detail(slug: str) -> dict | None:
    """The alliance and its active member units, for the public page."""
    with registry_session() as s:
        alliance = s.query(Alliance).filter(Alliance.slug == slug).one_or_none()
        if alliance is None:
            return None
        return {
            "id": alliance.id, "name": alliance.name, "slug": alliance.slug,
            "description": alliance.description, "created_at": alliance.created_at,
            "members": _active_members(s, alliance.id),
        }


def alliances_map(slugs: list[str]) -> dict[str, list[dict]]:
    """For directory badges: each unit slug → the alliances it's active in."""
    if not slugs:
        return {}
    with registry_session() as s:
        rows = (
            s.query(AllianceMember)
            .filter(AllianceMember.unit_slug.in_(slugs),
                    AllianceMember.status == "active")
            .all()
        )
        by_alliance = {}
        out: dict[str, list[dict]] = {}
        for r in rows:
            alliance = by_alliance.get(r.alliance_id)
            if alliance is None:
                alliance = s.get(Alliance, r.alliance_id)
                by_alliance[r.alliance_id] = alliance
            if alliance is not None:
                out.setdefault(r.unit_slug, []).append(
                    {"name": alliance.name, "slug": alliance.slug})
        return out
