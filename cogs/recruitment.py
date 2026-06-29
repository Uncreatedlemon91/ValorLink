import discord
from discord import app_commands
from discord.ext import commands

from db.base import SessionLocal
from db.models import Member, ServiceHistoryEntry
from utils import ranks as rank_utils
from utils.checks import is_recruiter
from utils.embeds import base_embed
from utils.settings import default_company_name, get_config
from utils.sync import sync_company, sync_rank


class InterviewView(discord.ui.View):
    """Sent to the candidate's private interview thread for recruiters to decide on."""

    def __init__(self, applicant_id: int, callsign: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.callsign = callsign

    def _is_recruiter(self, member: discord.Member) -> bool:
        with SessionLocal() as session:
            cfg = get_config(session)
        ids = {cfg.admin_role_id, cfg.officer_role_id, cfg.recruiter_role_id}
        return any(r.id in ids for r in member.roles)

    @discord.ui.button(label="Approve Enlistment", style=discord.ButtonStyle.green, custom_id="recruit_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_recruiter(interaction.user):
            return await interaction.response.send_message("You're not authorized to do that.", ephemeral=True)

        await interaction.response.defer()
        applicant = interaction.guild.get_member(self.applicant_id)
        if applicant is None:
            return await interaction.edit_original_response(content="Applicant has left the server.", view=None)

        with SessionLocal() as session:
            cfg = get_config(session)
            candidate_role_id = cfg.candidate_role_id
            member_role_id = cfg.member_role_id
            personnel_forum_id = cfg.personnel_forum_id
            admin_log_channel_id = cfg.admin_log_channel_id
            regiment_name = cfg.regiment_name
            default_rank = rank_utils.default_rank_name(session)
            default_company = default_company_name(session)

        candidate_role = interaction.guild.get_role(candidate_role_id) if candidate_role_id else None
        member_role = interaction.guild.get_role(member_role_id) if member_role_id else None
        try:
            if candidate_role:
                await applicant.remove_roles(candidate_role)
            if member_role:
                await applicant.add_roles(member_role)
        except discord.HTTPException:
            pass

        thread_id = None
        forum = interaction.guild.get_channel(personnel_forum_id) if personnel_forum_id else None
        if isinstance(forum, discord.ForumChannel):
            created = await forum.create_thread(
                name=f"{self.callsign}",
                content=f"**Personnel Dossier: {self.callsign}**\nEnlisted by {interaction.user.mention}.",
            )
            thread_id = created.thread.id

        with SessionLocal() as session:
            session.merge(
                Member(
                    discord_id=applicant.id,
                    callsign=self.callsign,
                    rank=default_rank,
                    company=default_company,
                    status="active",
                    thread_id=thread_id,
                )
            )
            session.add(
                ServiceHistoryEntry(
                    member_id=applicant.id,
                    entry=f"Enlisted as {default_rank} via recruitment pipeline.",
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        await sync_rank(applicant, self.callsign, None, default_rank)
        await sync_company(applicant, None, default_company)

        try:
            from cogs.roster import refresh_roster

            await refresh_roster(interaction.guild)
        except Exception:
            pass

        log_channel = interaction.guild.get_channel(admin_log_channel_id) if admin_log_channel_id else None
        if log_channel:
            embed = base_embed(title="Enlistment Approved", color=discord.Color.green().value)
            embed.add_field(name="Applicant", value=applicant.mention)
            embed.add_field(name="Approved By", value=interaction.user.mention)
            embed.set_footer(text=f"ID: {applicant.id}")
            await log_channel.send(embed=embed)

        try:
            await applicant.send(
                f"Your application to **{regiment_name}** has been approved. Welcome to the regiment!"
            )
        except discord.Forbidden:
            pass

        await interaction.edit_original_response(
            content=f"Enlistment complete. {applicant.mention} is now active duty.", view=None
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, custom_id="recruit_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_recruiter(interaction.user):
            return await interaction.response.send_message("You're not authorized to do that.", ephemeral=True)

        await interaction.response.defer()
        applicant = interaction.guild.get_member(self.applicant_id)

        with SessionLocal() as session:
            cfg = get_config(session)
            admin_log_channel_id = cfg.admin_log_channel_id
            regiment_name = cfg.regiment_name

        log_channel = interaction.guild.get_channel(admin_log_channel_id) if admin_log_channel_id else None
        if log_channel:
            embed = base_embed(title="Enlistment Denied", color=discord.Color.red().value)
            embed.add_field(name="Applicant", value=applicant.mention if applicant else str(self.applicant_id))
            embed.add_field(name="Denied By", value=interaction.user.mention)
            await log_channel.send(embed=embed)

        if applicant:
            try:
                await applicant.send(
                    f"Your application to **{regiment_name}** has been denied at this time."
                )
            except discord.Forbidden:
                pass

        await interaction.edit_original_response(content="Candidate denied.", view=None)


class ApplyModal(discord.ui.Modal, title="Regiment Application"):
    callsign = discord.ui.TextInput(label="In-Game Name / Callsign", placeholder="e.g. Smith")
    age = discord.ui.TextInput(label="Age", placeholder="Must be 18+", min_length=1, max_length=3)
    timezone = discord.ui.TextInput(label="Timezone", placeholder="e.g. EST / GMT")
    reason = discord.ui.TextInput(label="Why do you want to join?", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        with SessionLocal() as session:
            cfg = get_config(session)
            candidate_role_id = cfg.candidate_role_id
            recruiter_role_id = cfg.recruiter_role_id

        candidate_role = interaction.guild.get_role(candidate_role_id) if candidate_role_id else None
        if candidate_role:
            try:
                await interaction.user.add_roles(candidate_role)
            except discord.HTTPException:
                pass

        thread = await interaction.channel.create_thread(
            name=f"Interview - {self.callsign.value}",
            type=discord.ChannelType.private_thread,
            invitable=False,
        )

        recruiter_role = interaction.guild.get_role(recruiter_role_id) if recruiter_role_id else None
        ping = f"{interaction.user.mention} {recruiter_role.mention if recruiter_role else ''}".strip()

        embed = base_embed(title="New Application")
        embed.add_field(name="Applicant", value=interaction.user.mention, inline=True)
        embed.add_field(name="Callsign", value=self.callsign.value, inline=True)
        embed.add_field(name="Age", value=self.age.value, inline=True)
        embed.add_field(name="Timezone", value=self.timezone.value, inline=True)
        embed.add_field(name="Reason", value=self.reason.value, inline=False)

        await thread.send(content=ping, embed=embed, view=InterviewView(interaction.user.id, self.callsign.value))
        await interaction.followup.send(f"Application received. Continue in {thread.mention}.", ephemeral=True)


class JoinButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply to Enlist", style=discord.ButtonStyle.primary, custom_id="recruit_apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplyModal())


class Recruitment(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup_recruitment", description="Post the persistent enlistment application button")
    @is_recruiter()
    async def setup_recruitment(self, interaction: discord.Interaction):
        with SessionLocal() as session:
            regiment_name = get_config(session).regiment_name

        embed = base_embed(
            title=f"{regiment_name} Recruitment",
            description="Click below to submit your application and begin the enlistment process.",
        )
        await interaction.channel.send(embed=embed, view=JoinButtonView())
        await interaction.response.send_message("Recruitment post deployed.", ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_view(JoinButtonView())
    await bot.add_cog(Recruitment(bot))
