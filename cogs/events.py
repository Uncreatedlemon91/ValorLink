from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from db.base import SessionLocal
from db.models import AttendanceRecord, Event, Member
from utils.checks import is_officer
from utils.embeds import base_embed
from utils.settings import get_config

EVENT_TYPES = ["Drill", "Battle", "Operation"]
RSVP_STATUSES = {"accepted": "Accepted", "declined": "Declined", "tentative": "Tentative"}
ATTENDANCE_STATUSES = ["present", "absent", "excused"]


def _rsvp_counts(session, event_id: int) -> dict[str, int]:
    counts = {k: 0 for k in RSVP_STATUSES}
    for record in session.query(AttendanceRecord).filter(AttendanceRecord.event_id == event_id):
        if record.status in counts:
            counts[record.status] += 1
    return counts


def _build_event_embed(event: Event, counts: dict[str, int]) -> discord.Embed:
    embed = base_embed(
        title=f"{event.event_type}: {event.name}",
        description=f"**When:** {event.scheduled_at.strftime('%Y-%m-%d %H:%M UTC')}",
    )
    embed.add_field(name="Accepted", value=str(counts["accepted"]), inline=True)
    embed.add_field(name="Tentative", value=str(counts["tentative"]), inline=True)
    embed.add_field(name="Declined", value=str(counts["declined"]), inline=True)
    return embed


class RSVPView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id

    async def _set_rsvp(self, interaction: discord.Interaction, status: str):
        with SessionLocal() as session:
            event = session.get(Event, self.event_id)
            if event is None:
                return await interaction.response.send_message("This event no longer exists.", ephemeral=True)

            member = session.get(Member, interaction.user.id)
            if member is None:
                return await interaction.response.send_message(
                    "Only enlisted members have an attendance record.", ephemeral=True
                )

            record = (
                session.query(AttendanceRecord)
                .filter(AttendanceRecord.event_id == self.event_id, AttendanceRecord.member_id == interaction.user.id)
                .one_or_none()
            )
            if record:
                record.status = status
                record.responded_at = datetime.utcnow()
            else:
                session.add(AttendanceRecord(event_id=self.event_id, member_id=interaction.user.id, status=status))

            member.last_active_date = datetime.utcnow()
            session.commit()

            counts = _rsvp_counts(session, self.event_id)
            embed = _build_event_embed(event, counts)

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"RSVP recorded: **{RSVP_STATUSES[status]}**.", ephemeral=True)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="event_rsvp_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_rsvp(interaction, "accepted")

    @discord.ui.button(label="Tentative", style=discord.ButtonStyle.grey, custom_id="event_rsvp_tentative")
    async def tentative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_rsvp(interaction, "tentative")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, custom_id="event_rsvp_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_rsvp(interaction, "declined")


async def event_type_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=t, value=t) for t in EVENT_TYPES if current.lower() in t.lower()]


async def event_name_autocomplete(interaction: discord.Interaction, current: str):
    with SessionLocal() as session:
        events = (
            session.query(Event)
            .filter(Event.scheduled_at >= datetime.utcnow() - timedelta(days=30))
            .order_by(Event.scheduled_at.desc())
            .limit(25)
            .all()
        )
        return [
            app_commands.Choice(name=f"{e.name} ({e.scheduled_at.strftime('%Y-%m-%d')})", value=str(e.id))
            for e in events
            if current.lower() in e.name.lower()
        ]


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="event_create", description="Announce a drill/battle/operation with RSVP")
    @app_commands.autocomplete(event_type=event_type_autocomplete)
    @is_officer()
    async def event_create(
        self,
        interaction: discord.Interaction,
        name: str,
        event_type: str,
        when: str,
    ):
        try:
            scheduled_at = datetime.strptime(when, "%Y-%m-%d %H:%M")
        except ValueError:
            return await interaction.response.send_message(
                "Invalid date format. Use `YYYY-MM-DD HH:MM` (UTC), e.g. `2026-07-01 19:00`.", ephemeral=True
            )

        with SessionLocal() as session:
            announcements_channel_id = get_config(session).announcements_channel_id
        channel = (
            (interaction.guild.get_channel(announcements_channel_id) if announcements_channel_id else None)
            or interaction.channel
        )

        with SessionLocal() as session:
            event = Event(
                name=name,
                event_type=event_type,
                scheduled_at=scheduled_at,
                created_by=interaction.user.id,
                channel_id=channel.id,
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            event_id = event.id
            counts = _rsvp_counts(session, event_id)
            embed = _build_event_embed(event, counts)

        view = RSVPView(event_id)
        message = await channel.send(embed=embed, view=view)

        with SessionLocal() as session:
            event = session.get(Event, event_id)
            event.message_id = message.id
            session.commit()

        await interaction.response.send_message(f"Event posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="attendance_mark", description="Mark a member's actual attendance for an event")
    @app_commands.autocomplete(event=event_name_autocomplete)
    @app_commands.choices(status=[app_commands.Choice(name=s, value=s) for s in ATTENDANCE_STATUSES])
    @is_officer()
    async def attendance_mark(
        self,
        interaction: discord.Interaction,
        event: str,
        member: discord.Member,
        status: app_commands.Choice[str],
    ):
        try:
            event_id = int(event)
        except ValueError:
            return await interaction.response.send_message("Select an event from the autocomplete list.", ephemeral=True)

        with SessionLocal() as session:
            event_row = session.get(Event, event_id)
            if event_row is None:
                return await interaction.response.send_message("Event not found.", ephemeral=True)

            record = (
                session.query(AttendanceRecord)
                .filter(AttendanceRecord.event_id == event_id, AttendanceRecord.member_id == member.id)
                .one_or_none()
            )
            if record:
                record.status = status.value
                record.responded_at = datetime.utcnow()
            else:
                session.add(AttendanceRecord(event_id=event_id, member_id=member.id, status=status.value))

            member_row = session.get(Member, member.id)
            if member_row:
                member_row.last_active_date = datetime.utcnow()

            session.commit()

        await interaction.response.send_message(f"Marked {member.mention} as **{status.value}** for {event_row.name}.")

    @app_commands.command(name="attendance_history", description="View a member's attendance history")
    async def attendance_history(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        with SessionLocal() as session:
            cfg = get_config(session)
        is_priv = any(r.id in (cfg.admin_role_id, cfg.officer_role_id) for r in interaction.user.roles)
        if target.id != interaction.user.id and not is_priv:
            return await interaction.response.send_message("You can only view your own attendance.", ephemeral=True)

        with SessionLocal() as session:
            records = (
                session.query(AttendanceRecord)
                .filter(AttendanceRecord.member_id == target.id)
                .order_by(AttendanceRecord.responded_at.desc())
                .limit(10)
                .all()
            )
            lines = []
            for r in records:
                event = session.get(Event, r.event_id)
                if event:
                    lines.append(f"- {event.scheduled_at.strftime('%Y-%m-%d')} {event.name}: **{r.status}**")

        embed = base_embed(title=f"Attendance History: {target.display_name}")
        embed.description = "\n".join(lines) if lines else "No attendance records yet."
        await interaction.response.send_message(embed=embed, ephemeral=not is_priv)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
