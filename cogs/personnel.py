import discord
from discord import app_commands
from discord.ext import commands

import config
from db.base import SessionLocal
from db.models import Member, ServiceHistoryEntry
from utils import ranks as rank_utils
from utils.checks import is_officer
from utils.embeds import base_embed
from utils.sync import sync_rank


async def rank_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in config.RANKS
        if current.lower() in r["name"].lower()
    ][:25]


class Personnel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _refresh_roster(self, guild: discord.Guild):
        try:
            from cogs.roster import refresh_roster

            await refresh_roster(guild)
        except Exception:
            pass

    @app_commands.command(name="promote", description="Promote a member to the next rank")
    @is_officer()
    async def promote(self, interaction: discord.Interaction, member: discord.Member):
        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            new_rank = rank_utils.next_rank(record.rank)
            if new_rank is None:
                return await interaction.response.send_message(f"{record.callsign} is already at the top rank.", ephemeral=True)

            old_rank = record.rank
            callsign = record.callsign
            record.rank = new_rank
            session.add(
                ServiceHistoryEntry(
                    member_id=member.id,
                    entry=f"Promoted from {old_rank} to {new_rank} by {interaction.user.display_name}.",
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_rank(member, callsign, old_rank, new_rank)
        await self._refresh_roster(interaction.guild)
        await interaction.response.send_message(f"{member.mention} promoted to **{new_rank}**.")

    @app_commands.command(name="demote", description="Demote a member to the previous rank")
    @is_officer()
    async def demote(self, interaction: discord.Interaction, member: discord.Member):
        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            new_rank = rank_utils.prev_rank(record.rank)
            if new_rank is None:
                return await interaction.response.send_message(f"{record.callsign} is already at the lowest rank.", ephemeral=True)

            old_rank = record.rank
            callsign = record.callsign
            record.rank = new_rank
            session.add(
                ServiceHistoryEntry(
                    member_id=member.id,
                    entry=f"Demoted from {old_rank} to {new_rank} by {interaction.user.display_name}.",
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_rank(member, callsign, old_rank, new_rank)
        await self._refresh_roster(interaction.guild)
        await interaction.response.send_message(f"{member.mention} demoted to **{new_rank}**.")

    @app_commands.command(name="set_rank", description="Set a member's rank directly")
    @app_commands.autocomplete(rank=rank_autocomplete)
    @is_officer()
    async def set_rank(self, interaction: discord.Interaction, member: discord.Member, rank: str):
        try:
            rank_utils.rank_index(rank)
        except ValueError:
            return await interaction.response.send_message(f"Unknown rank: {rank}", ephemeral=True)

        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            old_rank = record.rank
            callsign = record.callsign
            record.rank = rank
            session.add(
                ServiceHistoryEntry(
                    member_id=member.id,
                    entry=f"Rank set from {old_rank} to {rank} by {interaction.user.display_name}.",
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_rank(member, callsign, old_rank, rank)
        await self._refresh_roster(interaction.guild)
        await interaction.response.send_message(f"{member.mention}'s rank set to **{rank}**.")

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

        await interaction.response.send_message(f"Logged entry for {member.mention}.")

    @app_commands.command(name="record", description="View a member's personnel file")
    async def record(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        is_self = target.id == interaction.user.id
        is_priv = any(
            r.id in (config.ADMIN_ROLE_ID, config.OFFICER_ROLE_ID) for r in interaction.user.roles
        )
        if not is_self and not is_priv:
            return await interaction.response.send_message("You can only view your own record.", ephemeral=True)

        with SessionLocal() as session:
            record = session.get(Member, target.id)
            if record is None:
                return await interaction.response.send_message(f"{target.mention} has no personnel record.", ephemeral=True)

            history = sorted(record.service_history, key=lambda e: e.date, reverse=True)[:5]
            discipline = sorted(record.disciplinary_records, key=lambda d: d.date, reverse=True)[:5]
            awards = sorted(record.awards, key=lambda a: a.date_awarded, reverse=True)

            embed = base_embed(title=f"Personnel File: {record.callsign}")
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

        await interaction.response.send_message(embed=embed, ephemeral=not is_priv)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personnel(bot))
