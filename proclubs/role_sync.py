"""Keeps the Discord roles the site manages in step with the site.

The site is the source of truth for the roles in config.MANAGED_ROLE_IDS:

  Starter / Rotation / Substitute   the squad status on a live contract
  Squad                             anyone under contract (or whose
                                    accepted offer awaits confirmation)
  Club President / Head Coach / Coach   the club role on their player file
  Trialist                          a prospect On trial or Offered, who
                                    isn't under contract yet

A member should hold exactly the managed roles their site record says,
and sync_member makes it so: it adds what's missing and removes what no
longer applies. It never touches any role outside that list, and never
DISCORD_STAFF_ROLE_ID (excluded in config), so a mistake here can't lock
anybody out of the site or strip a role the club manages by hand.

Syncing one person happens after every change to them on the site (see
app.py and staff_routes.py). Bringing the whole server in line --
including people who hold a managed role the site knows nothing about --
is only ever done by management pressing Apply on /discord-roles, after
seeing exactly what would change.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
import discord_api
import discord_roster
from database import get_session
from models import Player, Prospect, RosterMove
from services import live_contract_for, offer_awaits_confirmation

DiscordApiError = discord_api.DiscordApiError

# What each managed key is called on the page, in the order shown.
LABELS = {
    "Club President": "Club President",
    "Head Coach": "Head Coach",
    "Coach": "Coach",
    "Squad": "Squad (under contract)",
    "Starter": "Starting players",
    "Rotation": "Rotation players",
    "Substitute": "Substitute players",
    "Trialist": "Trialists",
}
TRIAL_STAGES = ("trial", "offered")


def managed() -> dict[str, str]:
    """key -> role id, for the roles configured."""
    return dict(config.MANAGED_ROLE_IDS)


def desired_keys(session: Session, discord_id: str) -> set[str]:
    """The managed roles this person should hold, by key."""
    did = str(discord_id)
    keys: set[str] = set()
    contract = live_contract_for(session, did)
    if contract is not None:
        keys.add("Squad")
        if contract.squad_status:
            keys.add(contract.squad_status)
    else:
        offers = session.execute(select(RosterMove).where(
            RosterMove.discord_id == did, RosterMove.kind == discord_roster.MOVE_OFFER)).scalars()
        if any(offer_awaits_confirmation(m) for m in offers):
            keys.add("Squad")
        trialling = session.execute(select(Prospect.id).where(
            Prospect.discord_id == did, Prospect.stage.in_(TRIAL_STAGES))).first()
        if trialling:
            keys.add("Trialist")
    player = session.execute(select(Player).where(Player.discord_id == did)).scalar_one_or_none()
    if player is not None and player.club_role:
        keys.add(player.club_role)
    return keys


def desired_role_ids(session: Session, discord_id: str) -> set[str]:
    roles = managed()
    return {roles[k] for k in desired_keys(session, discord_id) if k in roles}


def changes_for(current: set[str], desired: set[str]) -> tuple[set[str], set[str]]:
    """(to add, to remove), both limited to managed role ids."""
    ours = set(managed().values())
    return (desired & ours) - current, (current & ours) - desired


def _key_for(role_id: str) -> str:
    return next((k for k, v in managed().items() if v == role_id), role_id)


def _member_roles(discord_id: str) -> set[str] | None:
    """The member's current roles, or None if they aren't in the server."""
    try:
        resp = discord_api.get(f"/guilds/{config.DISCORD_GUILD_ID}/members/{discord_id}")
    except DiscordApiError as exc:
        if "404" in str(exc):
            return None
        raise
    return {str(r) for r in resp.json().get("roles", [])}


def apply_changes(discord_id: str, add: set[str], remove: set[str]) -> None:
    base = f"/guilds/{config.DISCORD_GUILD_ID}/members/{discord_id}/roles"
    for role_id in sorted(add):
        discord_api.put(f"{base}/{role_id}")
    for role_id in sorted(remove):
        discord_api.delete(f"{base}/{role_id}")


def sync_member(session: Session, discord_id: str) -> dict:
    """Brings one member's managed roles in line with the site. Returns
    {"added": [keys], "removed": [keys], "in_server": bool}."""
    if not config.ROLE_SYNC_ENABLED or not str(discord_id).isdigit():
        return {"added": [], "removed": [], "in_server": True}
    current = _member_roles(str(discord_id))
    if current is None:
        return {"added": [], "removed": [], "in_server": False}
    add, remove = changes_for(current, desired_role_ids(session, discord_id))
    apply_changes(str(discord_id), add, remove)
    return {"added": sorted(_key_for(r) for r in add), "removed": sorted(_key_for(r) for r in remove),
            "in_server": True}


def sync_quietly(*discord_ids: str) -> str | None:
    """Syncs each person in its own session, after the change that called
    it has committed. Returns what went wrong, for a flash, or None. A
    role problem never undoes the change on the site."""
    if not config.ROLE_SYNC_ENABLED:
        return None
    problems = []
    for discord_id in discord_ids:
        if not discord_id:
            continue
        try:
            with get_session() as session:
                sync_member(session, str(discord_id))
        except DiscordApiError as exc:
            problems.append(str(exc))
    # The roster page's member list shows roles; don't serve a stale one.
    discord_roster.invalidate_members_cache()
    if not problems:
        return None
    return ("Saved, but Discord roles couldn't be updated: " + problems[0]
            + " (check the bot has Manage Roles and sits above these roles).")


# --------------------------------------------------------------------------- #
# The whole server, for /discord-roles
# --------------------------------------------------------------------------- #
def server_plan(session: Session, members: list[dict]) -> list[dict]:
    """Every member whose managed roles differ from what the site says:
    [{id, name, add: [keys], remove: [keys]}]. `members` is the guild
    member list (discord_roster.guild_members)."""
    ours = set(managed().values())
    rows = []
    for m in members:
        user = m.get("user") or {}
        uid = str(user.get("id") or "")
        if not uid or user.get("bot"):
            continue
        current = {str(r) for r in m.get("roles") or []}
        desired = desired_role_ids(session, uid)
        if not (current & ours) and not desired:
            continue
        add, remove = changes_for(current, desired)
        if add or remove:
            rows.append({"id": uid, "name": discord_roster.display_name(m),
                         "add": sorted(_key_for(r) for r in add),
                         "remove": sorted(_key_for(r) for r in remove),
                         "add_ids": sorted(add), "remove_ids": sorted(remove)})
    return sorted(rows, key=lambda r: r["name"].casefold())


def holders(members: list[dict]) -> dict[str, list[str]]:
    """key -> names of members holding that managed role in Discord now."""
    out = {k: [] for k in managed()}
    by_id = {v: k for k, v in managed().items()}
    for m in members:
        for r in m.get("roles") or []:
            key = by_id.get(str(r))
            if key:
                out[key].append(discord_roster.display_name(m))
    return {k: sorted(v, key=str.casefold) for k, v in out.items()}


def apply_plan(rows: list[dict]) -> tuple[int, list[str]]:
    """Applies a server plan. Returns (members changed, errors)."""
    changed, errors = 0, []
    for row in rows:
        try:
            apply_changes(row["id"], set(row["add_ids"]), set(row["remove_ids"]))
            changed += 1
        except DiscordApiError as exc:
            errors.append(f"{row['name']}: {exc}")
    return changed, errors
