import discord
from discord import app_commands
from discord.ext import commands

from db.base import SessionLocal
from db.models import Member, ServiceHistoryEntry
from utils import ranks as rank_utils
from utils.billboard import post_billboard
from utils.checks import is_officer
from utils.embeds import base_embed
from utils.settings import get_config
from utils.sync import sync_rank


async def rank_autocomplete(interaction: discord.Interaction, current: str):
    with SessionLocal() as session:
        names = rank_utils.rank_names(session)
    return [
        app_commands.Choice(name=name, value=name) for name in names if current.lower() in name.lower()
    ][:25]


def _build_personnel_embed(record: Member, display_name: str) -> discord.Embed:
    """Build the personnel file embed from a loaded Member ORM object.
    Must be called while the session that loaded `record` is still open.
    """
    history = sorted(record.service_history, key=lambda e: e.date, reverse=True)[:5]
    discipline = sorted(record.disciplinary_records, key=lambda d: d.date, reverse=True)[:5]
    awards = sorted(record.awards, key=lambda a: a.date_awarded, reverse=True)

    embed = base_embed(title=f"Personnel File: {display_name}")
    embed.add_field(name="Rank", value=record.rank, inline=True)
    embed.add_field(name="Company", value=record.company, inline=True)
    embed.add_field(name="Status", value=record.status, inline=True)
    embed.add_field(name="Joined", value=record.joined_date.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Last Active", value=record.last_active_date.strftime("%Y-%m-%d"), inline=True)

    if awards:
        embed.add_field(
            name="Awards & Qualifications",
            value="\n".join(
                f"- {(a.award_type.emoji + ' ') if a.award_type.emoji else ''}{a.award_type.name} "
                f"({a.date_awarded.strftime('%Y-%m-%d')})"
                for a in awards
            ),
            inline=False,
        )

    if history:
        embed.add_field(
            name="Recent Service History",
            value="\n".join(f"- {h.date.strftime('%Y-%m-%d')}: {h.entry}" for h in history),
            inline=False,
        )

    if discipline:
        embed.add_field(
            name="Recent Disciplinary Records",
            value="\n".join(
                f"- {d.date.strftime('%Y-%m-%d')} [{d.record_type.upper()}]: {d.reason}" for d in discipline
            ),
            inline=False,
        )

    return embed


async def refresh_personnel_file(guild: discord.Guild, discord_id: int):
    """Re-render the personnel file embed in the member's dossier thread.

    Edits the thread's starter message (whose ID equals the thread ID in forum
    channels). Falls back to posting a new message if the starter can't be
    edited (e.g. it was deleted or the thread pre-dates this feature).
    """
    discord_member = guild.get_member(discord_id)
    display_name = discord_member.display_name if discord_member else None

    with SessionLocal() as session:
        record = session.get(Member, discord_id)
        if record is None or not record.thread_id:
            return
        display_name = display_name or record.callsign
        embed = _build_personnel_embed(record, display_name)
        thread_id = record.thread_id

    thread = guild.get_channel_or_thread(thread_id)
    if thread is None:
        try:
            thread = await guild.fetch_channel(thread_id)
        except discord.HTTPException:
            return

    # In forum channels the starter message ID equals the thread ID.
    try:
        msg = await thread.fetch_message(thread_id)
        await msg.edit(content=None, embed=embed)
    except discord.HTTPException:
        try:
            await thread.send(embed=embed)
        except discord.HTTPException:
            pass


class Personnel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _refresh_roster(self, guild: discord.Guild):
        try:
            from cogs.roster import refresh_roster
            await refresh_roster(guild)
        except Exception:
            pass

    @app_commands.command(name="promote", description="Change a member's rank up or down with a citation")
    @app_commands.autocomplete(rank=rank_autocomplete)
    @is_officer()
    async def promote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        citation: str = "",
    ):
        with SessionLocal() as session:
            new_record = rank_utils.rank_by_name(session, rank)
            if new_record is None:
                return await interaction.response.send_message(f"Unknown rank: {rank}", ephemeral=True)

            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            old_rank = record.rank
            callsign = record.callsign

            if old_rank == rank:
                return await interaction.response.send_message(
                    f"{callsign} already holds **{rank}**.", ephemeral=True
                )

            old_record = rank_utils.rank_by_name(session, old_rank)
            old_position = old_record.position if old_record else -1
            is_promotion = new_record.position > old_position

            record.rank = rank
            log_verb = "Promoted" if is_promotion else "Stepped down"
            entry_text = f"{log_verb} from {old_rank} to {rank} by {interaction.user.display_name}."
            if citation:
                entry_text += f" Citation: {citation}"
            session.add(
                ServiceHistoryEntry(
                    member_id=member.id,
                    entry=entry_text,
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_rank(member, callsign, old_rank, rank)
        await self._refresh_roster(interaction.guild)
        await refresh_personnel_file(interaction.guild, member.id)

        if is_promotion:
            msg = f"{callsign} has been promoted to **{rank}**."
            if citation:
                msg += f"\nCitation: {citation}"
            billboard_msg = f"**{callsign}** has been promoted to **{rank}**."
            if citation:
                billboard_msg += f" {citation}"
        else:
            msg = f"{callsign} has stepped down from their position as **{old_rank}**."
            if citation:
                msg += f" {citation}"
            billboard_msg = f"**{callsign}** has stepped down from the rank of **{old_rank}**."
            if citation:
                billboard_msg += f" {citation}"

        await post_billboard(interaction.guild, billboard_msg)
        await interaction.response.send_message(msg)

    @app_commands.command(name="set_rank", description="Silently correct a member's rank (no public announcement)")
    @app_commands.autocomplete(rank=rank_autocomplete)
    @is_officer()
    async def set_rank(self, interaction: discord.Interaction, member: discord.Member, rank: str, citation: str = ""):
        with SessionLocal() as session:
            if rank_utils.rank_by_name(session, rank) is None:
                return await interaction.response.send_message(f"Unknown rank: {rank}", ephemeral=True)

            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            old_rank = record.rank
            callsign = record.callsign
            record.rank = rank
            entry_text = f"Rank set from {old_rank} to {rank} by {interaction.user.display_name}."
            if citation:
                entry_text += f" {citation}"
            session.add(
                ServiceHistoryEntry(
                    member_id=member.id,
                    entry=entry_text,
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_rank(member, callsign, old_rank, rank)
        await self._refresh_roster(interaction.guild)
        await refresh_personnel_file(interaction.guild, member.id)
        await interaction.response.send_message(f"{member.mention}'s rank set to **{rank}**.", ephemeral=True)

    @app_commands.command(name="service_log", description="Add a service history entry to a member's record")
    @is_officer()
    async def service_log(self, interaction: discord.Interaction, member: discord.Member, entry: str):
        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            session.add(
                ServiceHistoryEntry(member_id=member.id, entry=entry, recorded_by=interaction.user.id)
            )
            session.commit()

        await refresh_personnel_file(interaction.guild, member.id)
        await interaction.response.send_message(f"Logged entry for {member.mention}.")

    @app_commands.command(name="record", description="View a member's personnel file")
    async def record(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        is_self = target.id == interaction.user.id

        with SessionLocal() as session:
            cfg = get_config(session)
            is_priv = any(r.id in (cfg.admin_role_id, cfg.officer_role_id) for r in interaction.user.roles)
            if not is_self and not is_priv:
                return await interaction.response.send_message("You can only view your own record.", ephemeral=True)

            record = session.get(Member, target.id)
            if record is None:
                return await interaction.response.send_message(f"{target.mention} has no personnel record.", ephemeral=True)

            embed = _build_personnel_embed(record, target.display_name)

        await interaction.response.send_message(embed=embed, ephemeral=not is_priv)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personnel(bot))
