import discord
from discord import app_commands
from discord.ext import commands

import config
from db.base import SessionLocal
from db.models import Member, ServiceHistoryEntry
from utils.checks import is_recruiter
from utils.embeds import base_embed


class InterviewView(discord.ui.View):
    """Sent to the candidate's private interview thread for recruiters to decide on."""

    def __init__(self, applicant_id: int, callsign: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.callsign = callsign

    def _is_recruiter(self, member: discord.Member) -> bool:
        ids = {config.ADMIN_ROLE_ID, config.OFFICER_ROLE_ID, config.RECRUITER_ROLE_ID}
        return any(r.id in ids for r in member.roles)

    @discord.ui.button(label="Approve Enlistment", style=discord.ButtonStyle.green, custom_id="recruit_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_recruiter(interaction.user):
            return await interaction.response.send_message("You're not authorized to do that.", ephemeral=True)

        await interaction.response.defer()
        applicant = interaction.guild.get_member(self.applicant_id)
        if applicant is None:
            return await interaction.edit_original_response(content="Applicant has left the server.", view=None)

        candidate_role = interaction.guild.get_role(config.CANDIDATE_ROLE_ID)
        member_role = interaction.guild.get_role(config.MEMBER_ROLE_ID)
        try:
            if candidate_role:
                await applicant.remove_roles(candidate_role)
            if member_role:
                await applicant.add_roles(member_role)
        except discord.HTTPException:
            pass

        thread_id = None
        forum = interaction.guild.get_channel(config.PERSONNEL_FORUM_ID)
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
                    rank=config.DEFAULT_RANK,
                    company=config.DEFAULT_COMPANY,
                    status="active",
                    thread_id=thread_id,
                )
            )
            session.add(
                ServiceHistoryEntry(
                    member_id=applicant.id,
                    entry=f"Enlisted as {config.DEFAULT_RANK} via recruitment pipeline.",
                    recorded_by=interaction.user.id,
                )
            )
            session.commit()

        try:
            from cogs.roster import refresh_roster

            await refresh_roster(interaction.guild)
        except Exception:
            pass

        log_channel = interaction.guild.get_channel(config.ADMIN_LOG_CHANNEL_ID)
        if log_channel:
            embed = base_embed(title="Enlistment Approved", color=discord.Color.green().value)
            embed.add_field(name="Applicant", value=applicant.mention)
            embed.add_field(name="Approved By", value=interaction.user.mention)
            embed.set_footer(text=f"ID: {applicant.id}")
            await log_channel.send(embed=embed)

        try:
            await applicant.send(
                f"Your application to **{config.REGIMENT_NAME}** has been approved. Welcome to the regiment!"
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

        log_channel = interaction.guild.get_channel(config.ADMIN_LOG_CHANNEL_ID)
        if log_channel:
            embed = base_embed(title="Enlistment Denied", color=discord.Color.red().value)
            embed.add_field(name="Applicant", value=applicant.mention if applicant else str(self.applicant_id))
            embed.add_field(name="Denied By", value=interaction.user.mention)
            await log_channel.send(embed=embed)

        if applicant:
            try:
                await applicant.send(
                    f"Your application to **{config.REGIMENT_NAME}** has been denied at this time."
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

        candidate_role = interaction.guild.get_role(config.CANDIDATE_ROLE_ID)
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

        recruiter_role = interaction.guild.get_role(config.RECRUITER_ROLE_ID)
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
        embed = base_embed(
            title=f"{config.REGIMENT_NAME} Recruitment",
            description="Click below to submit your application and begin the enlistment process.",
        )
        await interaction.channel.send(embed=embed, view=JoinButtonView())
        await interaction.response.send_message("Recruitment post deployed.", ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_view(JoinButtonView())
    await bot.add_cog(Recruitment(bot))
