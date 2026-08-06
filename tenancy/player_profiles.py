"""The player-authored half of a profile: bio, timezone, availability,
in-game names, and links -- one row per Discord identity.

Registry-backed because a player is the same person on every unit. The
read-only *service* half (postings, ranks, honors, turnout) is still
assembled per-unit by ``web/profiles.py``; this module owns only what the
member writes about themselves.

These fields used to be columns on each unit's Member row. ``import_legacy``
folds those old per-unit values in the first time a profile is touched, so
nobody loses a bio they wrote before the move.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from tenancy.registry import PlayerProfile, registry_session

DAY_CODES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

BIO_MAX_LEN = 1000

# Link platforms offered on the edit form, in display order.
LINK_PLATFORMS = [
    ("steam", "Steam"),
    ("twitch", "Twitch"),
    ("youtube", "YouTube"),
    ("website", "Website"),
]

_ALLOWED_SCHEMES = ("http://", "https://")
# "scheme:" at the very start, per RFC 3986. Used to tell "they left the
# https:// off" apart from "they gave a scheme we won't render".
_HAS_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _blank() -> dict:
    return {"bio": None, "timezone": None, "availability": [],
            "ingame_names": {}, "links": {}, "updated_at": None}


def _as_dict(row: PlayerProfile | None) -> dict:
    if row is None:
        return _blank()
    return {
        "bio": row.bio,
        "timezone": row.timezone,
        "availability": [d for d in (row.availability or "").split(",") if d],
        "ingame_names": _loads(row.ingame_names),
        "links": _loads(row.links),
        "updated_at": row.updated_at,
    }


def _clean_days(days) -> str | None:
    if isinstance(days, str):
        days = days.split(",")
    keep = [d for d in DAY_CODES if d in set(days or ())]  # canonical order
    return ",".join(keep) or None


def _clean_url(value: str) -> str | None:
    """Keep only plain http(s) URLs -- these are rendered as links on a public
    page, so a ``javascript:`` or ``data:`` value must never survive.

    A bare host ("twitch.tv/me") is assumed to be https. Anything that *does*
    carry a scheme has to already be http(s): prepending https:// to a
    rejected scheme would turn "javascript:alert(1)" into a URL that passes
    the check, so a bad scheme is dropped outright instead.
    """
    url = (value or "").strip()
    if not url:
        return None
    if _HAS_SCHEME_RE.match(url):
        return url[:300] if url.lower().startswith(_ALLOWED_SCHEMES) else None
    # Scheme-relative ("//evil.com") would survive the prepend as a valid but
    # unintended host, so strip the leading slashes first.
    return f"https://{url.lstrip('/')}"[:300]


def _clean_map(values: dict | None, limit: int = 120) -> dict:
    out = {}
    for key, value in (values or {}).items():
        key = (str(key) or "").strip()
        value = (str(value) or "").strip()
        if key and value:
            out[key[:60]] = value[:limit]
    return out


def get_profile(discord_id: int) -> dict:
    """This player's self-authored profile, importing any legacy per-unit
    values the first time it's read."""
    import_legacy(discord_id)
    with registry_session() as s:
        row = s.query(PlayerProfile).filter(
            PlayerProfile.discord_id == discord_id).one_or_none()
        return _as_dict(row)


def get_profiles(discord_ids) -> dict[int, dict]:
    """Bulk lookup, for pages that need many players' profiles at once (the
    roster's availability tally) without a query each. Does not import legacy
    values -- that happens when an individual profile is read or saved."""
    ids = [int(i) for i in discord_ids]
    if not ids:
        return {}
    with registry_session() as s:
        rows = s.query(PlayerProfile).filter(PlayerProfile.discord_id.in_(ids)).all()
        return {r.discord_id: _as_dict(r) for r in rows}


def save_profile(discord_id: int, bio: str = "", timezone: str = "",
                 availability=None, ingame_names: dict | None = None,
                 links: dict | None = None) -> str:
    import_legacy(discord_id)
    with registry_session() as s:
        row = s.query(PlayerProfile).filter(
            PlayerProfile.discord_id == discord_id).one_or_none()
        if row is None:
            row = PlayerProfile(discord_id=discord_id)
            s.add(row)
        row.bio = (bio or "").strip()[:BIO_MAX_LEN] or None
        row.timezone = (timezone or "").strip()[:60] or None
        row.availability = _clean_days(availability)
        row.ingame_names = json.dumps(_clean_map(ingame_names)) or None
        cleaned_links = {k: u for k, u in
                         ((k, _clean_url(v)) for k, v in _clean_map(links, limit=300).items())
                         if u}
        row.links = json.dumps(cleaned_links) or None
        s.commit()
    return "Your profile has been updated."


def import_legacy(discord_id: int) -> None:
    """Fold this player's old per-unit bio/timezone/in-game-name/availability
    into their platform profile, once.

    Prefers the unit they're still active in (their current details), falling
    back to any unit that has something recorded. Marked with ``imported_at``
    so a member who later clears a field doesn't have the old value pushed
    back in on the next read.
    """
    with registry_session() as s:
        row = s.query(PlayerProfile).filter(
            PlayerProfile.discord_id == discord_id).one_or_none()
        if row is not None and row.imported_at is not None:
            return

    # Imported lazily: this pulls in the per-unit model + tenant list, which
    # the registry layer otherwise has no business knowing about.
    from db.models import Member
    from tenancy.resolve import all_tenants
    from tenancy.units import sessionmaker_for

    with registry_session() as s:
        tenants = [(t.db_url, t.name, t.game) for t in all_tenants(s)]

    found: dict = {"bio": None, "timezone": None, "availability": None}
    ingame: dict[str, str] = {}
    for db_url, unit_name, game in tenants:
        try:
            with sessionmaker_for(db_url)() as us:
                member = us.get(Member, discord_id)
                if member is None:
                    continue
                # An active posting wins; otherwise take the first thing found.
                active = member.status == "active"
                for key in ("bio", "timezone", "availability"):
                    value = getattr(member, key, None)
                    if value and (found[key] is None or active):
                        found[key] = value
                if member.ingame_name:
                    # Keyed by the unit's game so several games can coexist;
                    # units with no game set fall back to the unit's name.
                    ingame.setdefault(game or unit_name, member.ingame_name)
        except Exception:  # noqa: BLE001 -- an unreadable unit must not block the import
            continue

    with registry_session() as s:
        row = s.query(PlayerProfile).filter(
            PlayerProfile.discord_id == discord_id).one_or_none()
        if row is None:
            row = PlayerProfile(discord_id=discord_id)
            s.add(row)
        elif row.imported_at is not None:  # another request beat us to it
            return
        if found["bio"] and not row.bio:
            row.bio = found["bio"][:BIO_MAX_LEN]
        if found["timezone"] and not row.timezone:
            row.timezone = found["timezone"][:60]
        if found["availability"] and not row.availability:
            row.availability = _clean_days(found["availability"])
        if ingame and not row.ingame_names:
            row.ingame_names = json.dumps(_clean_map(ingame))
        row.imported_at = datetime.utcnow()
        s.commit()
