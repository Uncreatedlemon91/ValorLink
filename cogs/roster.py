import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from db.base import SessionLocal
from db.models import Member, ServiceHistoryEntry, Setting
from utils import ranks as rank_utils
from utils.checks import is_officer
from utils.embeds import base_embed
from utils.settings import get_config, list_companies
from utils.sync import sync_company

ROSTER_MESSAGE_KEY = "roster_message_id"
ACTIVITY_TOUCH_COOLDOWN = timedelta(hours=1)


def _get_setting(session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def _set_setting(session, key: str, value: str):
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


def _build_roster_embed(session) -> discord.Embed:
    cfg = get_config(session)
    members = session.query(Member).filter(Member.status == "active").all()
    embed = base_embed(
        title=f"{cfg.regiment_name} Roster",
        description=f"{len(members)} active member(s)",
    )

    by_company: dict[str, list[Member]] = {}
    for m in members:
        by_company.setdefault(m.company, []).append(m)

    rank_order = {name: i for i, name in enumerate(rank_utils.rank_names(session))}
    configured_companies = [c.name for c in list_companies(session)]
    company_order = configured_companies + [c for c in by_company if c not in configured_companies]

    if not members:
        embed.add_field(name="No active members", value="-", inline=False)

    for company in company_order:
        roster_members = by_company.get(company)
        if not roster_members:
            continue
        roster_members.sort(key=lambda m: rank_order.get(m.rank, -1), reverse=True)
        lines = [f"**{m.rank}** {m.callsign} <@{m.discord_id}>" for m in roster_members]
        embed.add_field(name=f"{company} ({len(roster_members)})", value="\n".join(lines)[:1024], inline=False)

    return embed


async def refresh_roster(guild: discord.Guild):
    with SessionLocal() as session:
        roster_channel_id = get_config(session).roster_channel_id
        channel = guild.get_channel(roster_channel_id) if roster_channel_id else None
        if channel is None:
            return

        embed = _build_roster_embed(session)
        message_id = _get_setting(session, ROSTER_MESSAGE_KEY)

        message = None
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.HTTPException):
                message = None

        if message:
            await message.edit(embed=embed)
        else:
            message = await channel.send(embed=embed)
            _set_setting(session, ROSTER_MESSAGE_KEY, str(message.id))

        session.commit()


async def company_autocomplete(interaction: discord.Interaction, current: str):
    with SessionLocal() as session:
        names = [c.name for c in list_companies(session)]
    return [
        app_commands.Choice(name=c, value=c) for c in names if current.lower() in c.lower()
    ][:25]


class Roster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.inactivity_check.start()

    def cog_unload(self):
        self.inactivity_check.cancel()

    @app_commands.command(name="assign_company", description="Assign a member to a company")
    @app_commands.autocomplete(company=company_autocomplete)
    @is_officer()
    async def assign_company(self, interaction: discord.Interaction, member: discord.Member, company: str):
        with SessionLocal() as session:
            record = session.get(Member, member.id)
            if record is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            old_company = record.company
            record.company = company
            session.add(
                ServiceHistoryEntry(
                    member_id=member.id,
                    entry=f"Transferred from {old_company} to {company} by {interaction.user.display_name}.",
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_company(member, old_company, company)
        await refresh_roster(interaction.guild)
        await interaction.response.send_message(f"{member.mention} assigned to **{company}**.")

        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass

    @app_commands.command(name="roster", description="Force-refresh the live roster embed")
    @is_officer()
    async def roster_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await refresh_roster(interaction.guild)
        await interaction.followup.send("Roster refreshed.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        with SessionLocal() as session:
            record = session.get(Member, message.author.id)
            if record is None:
                return

            now = datetime.utcnow()
            if record.last_active_date and now - record.last_active_date < ACTIVITY_TOUCH_COOLDOWN:
                return

            record.last_active_date = now
            if record.status == "inactive":
                record.status = "active"
            session.commit()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick == after.nick:
            return

        # Strip the rank prefix the bot writes (e.g. "[Pvt] ") to get the bare callsign.
        raw = after.nick or after.name
        callsign = re.sub(r'^\[[^\]]+\]\s*', '', raw).strip()
        if not callsign:
            return

        with SessionLocal() as session:
            record = session.get(Member, after.id)
            if record is None or record.callsign == callsign:
                return
            record.callsign = callsign
            session.commit()

        await refresh_roster(after.guild)

    @tasks.loop(hours=24)
    async def inactivity_check(self):
        if not config.GUILD_ID:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return

        with SessionLocal() as session:
            cfg = get_config(session)
            threshold = datetime.utcnow() - timedelta(days=cfg.inactivity_days_threshold)
            stale = (
                session.query(Member)
                .filter(Member.status == "active", Member.last_active_date < threshold)
                .all()
            )
            flagged_callsigns = []
            flagged_ids = []
            for record in stale:
                record.status = "inactive"
                flagged_callsigns.append(record.callsign)
                flagged_ids.append(record.discord_id)
            session.commit()
            inactive_role_id = cfg.inactive_role_id
            admin_log_channel_id = cfg.admin_log_channel_id
            inactivity_days_threshold = cfg.inactivity_days_threshold

        if not flagged_ids:
            return

        inactive_role = guild.get_role(inactive_role_id) if inactive_role_id else None
        for discord_id in flagged_ids:
            member = guild.get_member(discord_id)
            if member and inactive_role:
                try:
                    await member.add_roles(inactive_role, reason="Flagged inactive by ValorLink")
                except discord.HTTPException:
                    pass

        log_channel = guild.get_channel(admin_log_channel_id) if admin_log_channel_id else None
        if log_channel:
            embed = base_embed(
                title="Inactivity Review",
                description=(
                    f"Flagged inactive (no activity in {inactivity_days_threshold}+ days):\n"
                    + "\n".join(f"- {name}" for name in flagged_callsigns)
                ),
            )
            await log_channel.send(embed=embed)

        await refresh_roster(guild)

    @inactivity_check.before_loop
    async def before_inactivity_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Roster(bot))
