from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from db.base import SessionLocal
from db.models import Company, Member, Rank, ServiceHistoryEntry
from utils.billboard import post_billboard
from utils.checks import is_officer
from utils.embeds import base_embed
from utils.settings import get_config


class Lifecycle(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loa_expiry_check.start()

    def cog_unload(self):
        self.loa_expiry_check.cancel()

    async def _strip_managed_roles(self, member: discord.Member):
        """Remove every bot-managed role from a discharged member."""
        with SessionLocal() as session:
            cfg = get_config(session)
            managed_ids = {cfg.member_role_id, cfg.candidate_role_id, cfg.inactive_role_id}
            for r in session.query(Rank).all():
                if r.role_id:
                    managed_ids.add(r.role_id)
            for c in session.query(Company).all():
                if c.role_id:
                    managed_ids.add(c.role_id)
        managed_ids.discard(None)
        to_remove = [r for r in member.roles if r.id in managed_ids]
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="Discharge")
            except discord.HTTPException:
                pass

    # --- /discharge ---

    @app_commands.command(name="discharge", description="Formally discharge a member from the regiment")
    @app_commands.choices(discharge_type=[
        app_commands.Choice(name="Honorable", value="honorable"),
        app_commands.Choice(name="Dishonorable", value="dishonorable"),
    ])
    @is_officer()
    async def discharge(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        discharge_type: app_commands.Choice[str],
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)

        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.followup.send("That member has no personnel record.", ephemeral=True)
            if record.status == "discharged":
                return await interaction.followup.send(f"{record.callsign} is already discharged.", ephemeral=True)

            callsign = record.callsign
            old_rank = record.rank
            thread_id = record.thread_id
            dtype = discharge_type.value

            record.status = "discharged"
            verb = "Honorably" if dtype == "honorable" else "Dishonorably"
            session.add(ServiceHistoryEntry(
                member_id=member.id,
                entry=f"{verb} discharged by {interaction.user.display_name}. Reason: {reason}",
                recorded_by=interaction.user.id,
            ))
            session.commit()

        await self._strip_managed_roles(member)

        # Update personnel file then lock the thread
        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass

        if thread_id:
            thread = interaction.guild.get_channel_or_thread(thread_id)
            if thread is None:
                try:
                    thread = await interaction.guild.fetch_channel(thread_id)
                except discord.HTTPException:
                    thread = None
            if thread:
                try:
                    await thread.edit(archived=True, locked=True, reason="Member discharged")
                except discord.HTTPException:
                    pass

        # Admin log
        with SessionLocal() as session:
            admin_log_id = get_config(session).admin_log_channel_id
        log_channel = interaction.guild.get_channel(admin_log_id) if admin_log_id else None
        if log_channel:
            color = discord.Color.green().value if dtype == "honorable" else discord.Color.red().value
            embed = base_embed(title=f"{verb} Discharge", color=color)
            embed.add_field(name="Member", value=member.mention, inline=True)
            embed.add_field(name="Rank at Discharge", value=old_rank, inline=True)
            embed.add_field(name="Discharged By", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            await log_channel.send(embed=embed)

        try:
            from cogs.roster import refresh_roster
            await refresh_roster(interaction.guild)
        except Exception:
            pass

        await post_billboard(
            interaction.guild,
            f"**{callsign}** has been {verb.lower()} discharged.",
        )

        await interaction.followup.send(
            f"**{callsign}** has been {verb.lower()} discharged.", ephemeral=True
        )

    # --- /loa ---

    @app_commands.command(name="loa", description="Place a member on leave of absence")
    @is_officer()
    async def loa(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Range[int, 1, 180],
        reason: str = "",
    ):
        loa_until = datetime.utcnow() + timedelta(days=days)

        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)
            if record.status == "loa":
                return await interaction.response.send_message(
                    f"{record.callsign} is already on leave.", ephemeral=True
                )
            if record.status == "discharged":
                return await interaction.response.send_message(
                    f"{record.callsign} has been discharged.", ephemeral=True
                )

            callsign = record.callsign
            record.status = "loa"
            record.loa_until = loa_until
            entry = f"Placed on leave of absence until {loa_until.strftime('%d %b %Y')} by {interaction.user.display_name}."
            if reason:
                entry += f" Reason: {reason}"
            session.add(ServiceHistoryEntry(member_id=member.id, entry=entry, recorded_by=interaction.user.id))
            session.commit()

        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass

        try:
            await member.send(
                f"You have been placed on leave of absence until **{loa_until.strftime('%d %b %Y')}**."
            )
        except discord.Forbidden:
            pass

        await post_billboard(
            interaction.guild,
            f"**{callsign}** is on leave of absence until {loa_until.strftime('%d %b %Y')}.",
        )
        await interaction.response.send_message(
            f"{member.mention} placed on leave until **{loa_until.strftime('%d %b %Y')}**.", ephemeral=True
        )

    # --- /loa_end ---

    @app_commands.command(name="loa_end", description="End a member's leave of absence early and return them to active duty")
    @is_officer()
    async def loa_end(self, interaction: discord.Interaction, member: discord.Member):
        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)
            if record.status != "loa":
                return await interaction.response.send_message(
                    f"{record.callsign} is not currently on leave.", ephemeral=True
                )

            callsign = record.callsign
            record.status = "active"
            record.loa_until = None
            record.last_active_date = datetime.utcnow()
            session.add(ServiceHistoryEntry(
                member_id=member.id,
                entry=f"Returned from leave early (ended by {interaction.user.display_name}).",
                recorded_by=interaction.user.id,
            ))
            session.commit()

        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass

        await post_billboard(interaction.guild, f"**{callsign}** has returned from leave of absence.")
        await interaction.response.send_message(f"{member.mention} is back on active duty.", ephemeral=True)

    # --- Daily LOA expiry task ---

    @tasks.loop(hours=24)
    async def loa_expiry_check(self):
        if not config.GUILD_ID:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return

        with SessionLocal() as session:
            expired = (
                session.query(Member)
                .filter(Member.status == "loa", Member.loa_until <= datetime.utcnow())
                .all()
            )
            returned = []
            bot_id = self.bot.user.id if self.bot.user else 0
            for record in expired:
                record.status = "active"
                record.loa_until = None
                record.last_active_date = datetime.utcnow()
                session.add(ServiceHistoryEntry(
                    member_id=record.discord_id,
                    entry="Returned from leave of absence (leave period ended).",
                    recorded_by=bot_id,
                ))
                returned.append((record.discord_id, record.callsign))
            session.commit()

        for discord_id, callsign in returned:
            await post_billboard(guild, f"**{callsign}**'s leave of absence has ended.")
            try:
                from cogs.personnel import refresh_personnel_file
                await refresh_personnel_file(guild, discord_id)
            except Exception:
                pass

    @loa_expiry_check.before_loop
    async def before_loa_expiry_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Lifecycle(bot))
