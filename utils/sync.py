"""Keeps a member's Discord role and nickname in sync with their rank and
company in the database. The bot's own role must sit above every rank/
company role in the server's role list, with Manage Roles and Manage
Nicknames permissions, or these calls silently no-op on discord.HTTPException.
"""
import discord

from db.base import SessionLocal
from utils import ranks as rank_utils
from utils.settings import company_by_name


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
    """Swap the member's rank role and refresh their nickname to "Abbr. Callsign"."""
    with SessionLocal() as session:
        old_record = rank_utils.rank_by_name(session, old_rank)
        new_record = rank_utils.rank_by_name(session, new_rank)
        old_role_id = (old_record.role_id or 0) if old_record else 0
        new_role_id = (new_record.role_id or 0) if new_record else 0
        abbreviation = new_record.abbreviation if new_record else new_rank

    await _swap_role(member, old_role_id, new_role_id, reason="Rank sync")

    try:
        await member.edit(nick=f"{abbreviation}. {callsign}")
    except discord.HTTPException:
        pass


async def sync_company(member: discord.Member, old_company: str | None, new_company: str):
    """Swap the member's company role to match their new company assignment."""
    with SessionLocal() as session:
        old_record = company_by_name(session, old_company)
        new_record = company_by_name(session, new_company)
        old_role_id = (old_record.role_id or 0) if old_record else 0
        new_role_id = (new_record.role_id or 0) if new_record else 0

    await _swap_role(member, old_role_id, new_role_id, reason="Company sync")
