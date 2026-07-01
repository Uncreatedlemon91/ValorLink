import discord
from discord import app_commands
from discord.ext import commands

from db.base import SessionLocal
from db.models import DisciplinaryRecord, Member
from utils.checks import is_officer
from utils.embeds import base_embed
from utils.settings import get_config

RECORD_COLORS = {"note": discord.Color.light_grey(), "warn": discord.Color.orange(), "strike": discord.Color.red()}


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _issue(self, interaction: discord.Interaction, member: discord.Member, record_type: str, reason: str):
        with SessionLocal() as session:
            target = session.get(Member, member.id)
            if target is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            session.add(
                DisciplinaryRecord(
                    member_id=member.id,
                    record_type=record_type,
                    reason=reason,
                    issued_by=interaction.user.id,
                )
            )
            session.commit()
            strike_count = sum(1 for d in target.disciplinary_records if d.record_type == "strike")

        with SessionLocal() as session:
            mod_log_channel_id = get_config(session).mod_log_channel_id
        log_channel = interaction.guild.get_channel(mod_log_channel_id) if mod_log_channel_id else None
        if log_channel:
            embed = base_embed(
                title=f"Disciplinary {record_type.capitalize()}",
                color=RECORD_COLORS[record_type].value,
            )
            embed.add_field(name="Member", value=member.mention, inline=True)
            embed.add_field(name="Issued By", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            if record_type == "strike":
                embed.add_field(name="Total Strikes", value=str(strike_count), inline=True)
            await log_channel.send(embed=embed)

        try:
            await member.send(f"You received a **{record_type}** in {interaction.guild.name}: {reason}")
        except discord.Forbidden:
            pass

        await interaction.response.send_message(f"{record_type.capitalize()} issued to {member.mention}.")

        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass

    @app_commands.command(name="note", description="Add an informal note to a member's disciplinary record")
    @is_officer()
    async def note(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await self._issue(interaction, member, "note", reason)

    @app_commands.command(name="warn", description="Issue a formal warning to a member")
    @is_officer()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await self._issue(interaction, member, "warn", reason)

    @app_commands.command(name="strike", description="Issue a strike against a member")
    @is_officer()
    async def strike(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await self._issue(interaction, member, "strike", reason)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
