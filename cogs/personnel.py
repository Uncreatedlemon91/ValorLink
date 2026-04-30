import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
from datetime import datetime

# --- 2-17 DELTA HQ CONFIGURATION ---
ADMIN_ROLE_ID = 1488703400349794495
RECRUITER_ROLE_ID = 1488320184275046471
PERSONNEL_FORUM_ID = 1499113777692672072

# --- NEW WORKFLOW CONFIG (Input your IDs here) ---
CANDIDATE_ROLE_ID = 1462215689090896054
CORPORAL_ROLE_ID = 1499138729502048418
RECRUITMENT_GROUP_TAG_ID = 1488320184275046471
ADMIN_LOG_CHANNEL_ID = 1499138882187559064

class InterviewView(discord.ui.View):
    """View sent to the private thread for Recruiters to Approve/Deny."""
    def __init__(self, applicant: discord.Member, callsign: str):
        super().__init__(timeout=None)
        self.applicant = applicant
        self.callsign = callsign

    @discord.ui.button(label="APPROVE ENLISTMENT", style=discord.ButtonStyle.green, custom_id="int_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if RECRUITER_ROLE_ID not in [r.id for r in interaction.user.roles]:
            return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)

        await interaction.response.defer()

        # 1. Update Discord Status
        try:
            candidate_role = interaction.guild.get_role(CANDIDATE_ROLE_ID)
            corporal_role = interaction.guild.get_role(CORPORAL_ROLE_ID)
            group_tag = interaction.guild.get_role(RECRUITMENT_GROUP_TAG_ID)

            if candidate_role: await self.applicant.remove_roles(candidate_role)
            if corporal_role: await self.applicant.add_roles(corporal_role)
            if group_tag: await self.applicant.add_roles(group_tag)
            
            # Nickname change to Corporal [CPL]
            await self.applicant.edit(nick=f"[CPL] {self.callsign}")
        except Exception as e:
            print(f"Role/Nick Error: {e}")

        # 2. Open Official Forum Dossier
        forum = interaction.guild.get_channel(PERSONNEL_FORUM_ID)
        thread_result = await forum.create_thread(
            name=f"[CPL] {self.callsign}",
            content=f"**OFFICIAL DOSSIER: {self.callsign.upper()}**\nEnlisted and Verified by {interaction.user.mention}."
        )

        # 3. Database Entry
        conn = sqlite3.connect('milsim_hq.db')
        c = conn.cursor()
        joined = datetime.now().strftime('%Y-%m-%d')
        c.execute("""INSERT INTO personnel 
                     (user_id, rank, points, assignment, joined_date, service_history, specialties, thread_id) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (self.applicant.id, "Corporal", 0, "Unassigned", joined, 
                   f"[{joined}] ENLISTED: Assigned CPL rank via Recruitment Pipeline.\n", "", thread_result.thread.id))
        conn.commit()
        conn.close()

        # 4. Admin Log
        log_channel = interaction.guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title="📝 ADMIN LOG: ENLISTMENT", color=discord.Color.green())
            log_embed.add_field(name="Operator", value=self.applicant.mention)
            log_embed.add_field(name="Approved By", value=interaction.user.mention)
            log_embed.set_footer(text=f"ID: {self.applicant.id}")
            await log_channel.send(embed=log_embed)

        # 5. DM User
        try:
            await self.applicant.send(f"⚡ **WELCOME TO 2-17 DELTA, CORPORAL.**\nYour enlistment is approved. Dossier: {thread_result.thread.jump_url}")
        except: pass

        await interaction.edit_original_response(content=f"✅ **ENLISTMENT COMPLETE.** Operator {self.callsign} moved to Active Duty.", view=None)

    @discord.ui.button(label="DENY & KICK", style=discord.ButtonStyle.red, custom_id="int_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if RECRUITER_ROLE_ID not in [r.id for r in interaction.user.roles]:
            return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)

        await interaction.response.defer()

        # 1. Admin Log
        log_channel = interaction.guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title="📝 ADMIN LOG: DENIAL", color=discord.Color.red())
            log_embed.add_field(name="Candidate", value=self.applicant.mention)
            log_embed.add_field(name="Denied By", value=interaction.user.mention)
            await log_channel.send(embed=log_embed)

        # 2. DM and Kick
        try:
            await self.applicant.send("❌ **2-17 DELTA HQ:** Your application has been denied at this time. You have been removed from the server.")
            await self.applicant.kick(reason="Enlistment Denied.")
        except: pass

        await interaction.edit_original_response(content=f"❌ **CANDIDATE DENIED AND REMOVED.**", view=None)

class JoinButtonView(discord.ui.View):
    """The persistent 'Join' button."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="JOIN 2-17 DELTA", style=discord.ButtonStyle.grey, custom_id="join_delta")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(Personnel.ApplyModal())

class Personnel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- MODAL: BASELINE INFORMATION ---
    class ApplyModal(discord.ui.Modal, title='SFOD-D Baseline Information'):
        callsign = discord.ui.TextInput(label='Requested Callsign', placeholder='e.g. Miller')
        age = discord.ui.TextInput(label='Age', placeholder='Must be 18+', min_length=2, max_length=2)
        timezone = discord.ui.TextInput(label='Timezone', placeholder='e.g. EST / GMT')
        reason = discord.ui.TextInput(label='Reason for Joining', style=discord.TextStyle.paragraph)

        async def on_submit(self, interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            
            # 1. Add Candidate Tag
            candidate_role = interaction.guild.get_role(CANDIDATE_ROLE_ID)
            if candidate_role: await interaction.user.add_roles(candidate_role)

            # 2. Create Private Thread for Interview
            # We use the channel the button was clicked in
            thread = await interaction.channel.create_thread(
                name=f"Interview - {self.callsign.value}",
                type=discord.ChannelType.private_thread,
                invitable=False
            )

            # 3. Join Recruiters and Ping
            recruiter_role = interaction.guild.get_role(RECRUITER_ROLE_ID)
            ping_msg = f"{interaction.user.mention} and {recruiter_role.mention if recruiter_role else 'Recruiters'}"
            
            # 4. Embed Answers
            embed = discord.Embed(title="📂 NEW APPLICATION SUBMITTED", color=0xf1c40f)
            embed.add_field(name="Candidate", value=interaction.user.mention, inline=True)
            embed.add_field(name="Callsign", value=self.callsign.value, inline=True)
            embed.add_field(name="Age", value=self.age.value, inline=True)
            embed.add_field(name="Timezone", value=self.timezone.value, inline=True)
            embed.add_field(name="Statement", value=self.reason.value, inline=False)
            
            await thread.send(content=ping_msg, embed=embed, view=InterviewView(interaction.user, self.callsign.value))
            await interaction.followup.send(f"Application received. Report to your interview thread: {thread.mention}", ephemeral=True)

    @app_commands.command(name="setup_recruitment", description="Post the persistent 'Join' button")
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def setup_recruitment(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="1ST SFOD-D RECRUITMENT",
            description="If you are looking to join the ranks of 2-17 Delta, click the button below to submit your baseline information and begin the selection process.",
            color=0x2f3136
        )
        embed.set_footer(text="SINE PARI")
        await interaction.channel.send(embed=embed, view=JoinButtonView())
        await interaction.response.send_message("Recruitment post deployed.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Personnel(bot))