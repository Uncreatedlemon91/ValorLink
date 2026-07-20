"""Keeps a member's Discord role and nickname in sync with their rank and
company in the database. The bot's own role must sit above every rank/
company role in the server's role list, with Manage Roles and Manage
Nicknames permissions, or these calls silently no-op on discord.HTTPException.

The nickname is composed as ``UnitTag CompanyTag Rank. Name`` — each tag is
optional, so a unit that sets neither keeps the historical ``Rank. Name``.
"""
import discord

from db.base import db_session
from db.models import Member
from utils import ranks as rank_utils
from utils.settings import company_by_name, get_config

# Discord caps nicknames at 32 characters.
DISCORD_NICK_MAX = 32


def build_nickname(unit_tag: str, company_tag: str, rank_abbr: str, callsign: str) -> str:
    """Assemble ``UnitTag CompanyTag Rank. Name`` from the pieces, dropping any
    empty tag and keeping within Discord's 32-character limit. The name is
    trimmed before the tags/rank so the prefix survives when space is tight."""
    prefix_parts = [p for p in (unit_tag.strip(), company_tag.strip()) if p]
    if rank_abbr:
        prefix_parts.append(f"{rank_abbr.strip()}.")
    prefix = " ".join(prefix_parts)
    if not prefix:
        return callsign[:DISCORD_NICK_MAX]
    room = DISCORD_NICK_MAX - len(prefix) - 1  # 1 for the space before the name
    if room <= 0:
        return prefix[:DISCORD_NICK_MAX]
    return f"{prefix} {callsign[:room]}".strip()


def _nickname_pieces(session, discord_id: int, callsign: str | None,
                     rank_name: str | None, company_name: str | None):
    """Resolve the tag/abbreviation pieces for a member. Any of callsign,
    rank, or company left as None is read from the stored record, so both the
    live sync path and the bulk resync path build an identical nickname."""
    record = session.get(Member, discord_id)
    if callsign is None:
        callsign = record.callsign if record else ""
    if rank_name is None:
        rank_name = record.rank if record else None
    if company_name is None:
        company_name = record.company if record else None

    cfg = get_config(session)
    unit_tag = cfg.unit_tag or ""
    rank_record = rank_utils.rank_by_name(session, rank_name) if rank_name else None
    rank_abbr = rank_record.abbreviation if rank_record else (rank_name or "")
    company_record = company_by_name(session, company_name) if company_name else None
    company_tag = (company_record.tag or "") if company_record else ""
    return unit_tag, company_tag, rank_abbr, callsign


async def _apply_nickname(member: discord.Member, callsign: str | None = None,
                          rank_name: str | None = None, company_name: str | None = None):
    with db_session() as session:
        unit_tag, company_tag, rank_abbr, name = _nickname_pieces(
            session, member.id, callsign, rank_name, company_name)
    nick = build_nickname(unit_tag, company_tag, rank_abbr, name)
    if not nick:
        return
    try:
        await member.edit(nick=nick)
    except discord.HTTPException:
        pass


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
    """Swap the member's rank role and rebuild their tagged nickname."""
    with db_session() as session:
        old_record = rank_utils.rank_by_name(session, old_rank)
        new_record = rank_utils.rank_by_name(session, new_rank)
        old_role_id = (old_record.role_id or 0) if old_record else 0
        new_role_id = (new_record.role_id or 0) if new_record else 0

    await _swap_role(member, old_role_id, new_role_id, reason="Rank sync")
    # Company/unit tags come from the stored record; the rank is authoritative
    # from the caller in case the record write hasn't landed on this path.
    await _apply_nickname(member, callsign=callsign, rank_name=new_rank)


async def sync_company(member: discord.Member, old_company: str | None, new_company: str):
    """Swap the member's company role and rebuild their tagged nickname (the
    company tag now forms part of the nickname)."""
    with db_session() as session:
        old_record = company_by_name(session, old_company)
        new_record = company_by_name(session, new_company)
        old_role_id = (old_record.role_id or 0) if old_record else 0
        new_role_id = (new_record.role_id or 0) if new_record else 0

    await _swap_role(member, old_role_id, new_role_id, reason="Company sync")
    await _apply_nickname(member, company_name=new_company)


async def resync_nickname(member: discord.Member):
    """Rebuild a member's nickname purely from stored data — used when a unit
    or company tag changes and every affected member needs refreshing."""
    await _apply_nickname(member)
