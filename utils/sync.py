"""Keeps a member's Discord role and nickname in sync with their rank and
company in the database. The bot's own role must sit above every rank/
company role in the server's role list, with Manage Roles and Manage
Nicknames permissions, or these calls silently no-op on discord.HTTPException.
"""
import discord

from config import COMPANY_ROLES
from utils import ranks as rank_utils


async def _swap_role(member: discord.Member, old_role_id: int, new_role_id: int, reason: str):
    guild = member.guild

    if old_role_id and old_role_id != new_role_id:
        old_role = guild.get_role(old_role_id)
        if old_role and old_role in member.roles:
            try:
                await member.remove_roles(old_role, reason=reason)
            except discord.HTTPException:
                pass

    if new_role_id:
        new_role = guild.get_role(new_role_id)
        if new_role and new_role not in member.roles:
            try:
                await member.add_roles(new_role, reason=reason)
            except discord.HTTPException:
                pass


async def sync_rank(member: discord.Member, callsign: str, old_rank: str | None, new_rank: str):
    """Swap the member's rank role and refresh their nickname to "[Abbr] Callsign"."""
    old_role_id = rank_utils.rank_by_name(old_rank).get("role_id", 0) if old_rank else 0
    new_role_id = rank_utils.rank_by_name(new_rank).get("role_id", 0)
    await _swap_role(member, old_role_id, new_role_id, reason="Rank sync")

    abbreviation = rank_utils.rank_by_name(new_rank)["abbreviation"]
    try:
        await member.edit(nick=f"[{abbreviation}] {callsign}")
    except discord.HTTPException:
        pass


async def sync_company(member: discord.Member, old_company: str | None, new_company: str):
    """Swap the member's company role to match their new company assignment."""
    old_role_id = COMPANY_ROLES.get(old_company, 0) if old_company else 0
    new_role_id = COMPANY_ROLES.get(new_company, 0)
    await _swap_role(member, old_role_id, new_role_id, reason="Company sync")
