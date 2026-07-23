from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from db.base import db_session
from db.models import AttendanceRecord, Event, EventSlot, Member
from tenancy.routing import bind_guild
from utils.billboard import _file_from_data_uri
from utils.checks import is_officer
from utils.embeds import base_embed, discord_ts
from utils.settings import get_config

EVENT_TYPES = ["Drill", "Battle", "Operation"]
RSVP_STATUSES = {"accepted": "Accepted", "declined": "Declined", "tentative": "Tentative"}
ATTENDANCE_STATUSES = ["present", "absent", "excused"]


def _rsvp_buckets(session, event_id: int) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {k: [] for k in RSVP_STATUSES}
    for record in session.query(AttendanceRecord).filter(AttendanceRecord.event_id == event_id):
        if record.status in buckets:
            member = session.get(Member, record.member_id)
            buckets[record.status].append(member.callsign if member else f"<@{record.member_id}>")
    return buckets


def _slot_counts(session, event_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    for record in session.query(AttendanceRecord).filter(
        AttendanceRecord.event_id == event_id, AttendanceRecord.status == "accepted",
        AttendanceRecord.slot_id.isnot(None),
    ):
        counts[record.slot_id] = counts.get(record.slot_id, 0) + 1
    return counts


def _build_event_embed(event: Event, buckets: dict[str, list[str]], slots: list[EventSlot] | None = None,
                       slot_counts: dict[int, int] | None = None):
    """Build the event announcement embed, plus a discord.File to send
    alongside it if the event has an image (Discord embeds can't reference a
    data: URI directly -- the image has to go up as an attachment). Returns
    (embed, file_or_None)."""
    slots = slots or []
    slot_counts = slot_counts or {}
    description = f"**When:** {discord_ts(event.scheduled_at, 'F')} ({discord_ts(event.scheduled_at, 'R')})"
    if event.description:
        description += f"\n\n{event.description}"
    embed = base_embed(
        title=f"{event.event_type}: {event.name}",
        description=description,
        color=event.color,
    )
    if slots:
        for slot in slots:
            filled = slot_counts.get(slot.id, 0)
            cap = f"{filled}/{slot.capacity}" if slot.capacity else f"{filled} signed up"
            embed.add_field(name=f"{slot.name} ({cap})", value="—", inline=True)
    else:
        embed.add_field(
            name=f"Accepted ({len(buckets['accepted'])})",
            value="\n".join(buckets["accepted"]) or "—",
            inline=True,
        )
    embed.add_field(
        name=f"Tentative ({len(buckets['tentative'])})",
        value="\n".join(buckets["tentative"]) or "—",
        inline=True,
    )
    embed.add_field(
        name=f"Declined ({len(buckets['declined'])})",
        value="\n".join(buckets["declined"]) or "—",
        inline=True,
    )
    file, name = _file_from_data_uri(event.image) if event.image else (None, None)
    if name:
        embed.set_image(url=f"attachment://{name}")
    return embed, file


class SlotSelect(discord.ui.Select):
    def __init__(self, event_id: int, slots: list[EventSlot], slot_counts: dict[int, int]):
        self.event_id = event_id
        super().__init__(
            placeholder="Choose a role to sign up…",
            custom_id=f"event_slot_select:{event_id}",
            options=self._options(slots, slot_counts),
        )

    @staticmethod
    def _options(slots: list[EventSlot], slot_counts: dict[int, int]) -> list[discord.SelectOption]:
        options = []
        for slot in slots:
            filled = slot_counts.get(slot.id, 0)
            cap = f"{filled}/{slot.capacity}" if slot.capacity else f"{filled} signed up"
            full = slot.capacity is not None and filled >= slot.capacity
            options.append(discord.SelectOption(
                label=f"{slot.name} ({cap})"[:100],
                value=str(slot.id),
                description="Full — pick another role" if full else None,
            ))
        return options or [discord.SelectOption(label="No roles configured", value="none")]

    async def callback(self, interaction: discord.Interaction):
        await self.view.set_slot(interaction, self.values[0])


class RSVPView(discord.ui.View):
    def __init__(self, event_id: int, slots: list[EventSlot] | None = None,
                slot_counts: dict[int, int] | None = None):
        super().__init__(timeout=None)
        self.event_id = event_id
        self.slots = slots or []
        if self.slots:
            for item in list(self.children):
                if getattr(item, "custom_id", None) == "event_rsvp_accept":
                    self.remove_item(item)
            self.add_item(SlotSelect(event_id, self.slots, slot_counts or {}))

    async def _rerender(self, session, event: Event):
        buckets = _rsvp_buckets(session, self.event_id)
        slots = (
            session.query(EventSlot).filter(EventSlot.event_id == self.event_id)
            .order_by(EventSlot.position).all()
        )
        counts = _slot_counts(session, self.event_id)
        for item in self.children:
            if isinstance(item, SlotSelect):
                item.options = item._options(slots, counts)
        return _build_event_embed(event, buckets, slots, counts)

    async def set_slot(self, interaction: discord.Interaction, slot_id_str: str):
        bind_guild(interaction.guild_id)
        with db_session() as session:
            event = session.get(Event, self.event_id)
            if event is None:
                return await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            try:
                slot_id = int(slot_id_str)
            except ValueError:
                return await interaction.response.send_message("That role no longer exists.", ephemeral=True)
            slot = session.get(EventSlot, slot_id)
            if slot is None or slot.event_id != self.event_id:
                return await interaction.response.send_message("That role no longer exists.", ephemeral=True)

            member = session.get(Member, interaction.user.id)
            if member is None:
                return await interaction.response.send_message(
                    "Only enlisted members have an attendance record.", ephemeral=True
                )

            if slot.capacity is not None:
                filled = (
                    session.query(AttendanceRecord)
                    .filter(AttendanceRecord.event_id == self.event_id,
                            AttendanceRecord.slot_id == slot_id,
                            AttendanceRecord.status == "accepted",
                            AttendanceRecord.member_id != interaction.user.id)
                    .count()
                )
                if filled >= slot.capacity:
                    return await interaction.response.send_message(
                        f"**{slot.name}** is full. Pick another role.", ephemeral=True
                    )

            record = (
                session.query(AttendanceRecord)
                .filter(AttendanceRecord.event_id == self.event_id, AttendanceRecord.member_id == interaction.user.id)
                .one_or_none()
            )
            if record:
                record.status = "accepted"
                record.slot_id = slot_id
                record.responded_at = datetime.utcnow()
            else:
                session.add(AttendanceRecord(event_id=self.event_id, member_id=interaction.user.id,
                                             status="accepted", slot_id=slot_id))
            member.last_active_date = datetime.utcnow()
            session.commit()

            embed, file = await self._rerender(session, event)
            slot_name = slot.name

        kwargs = {"embed": embed, "view": self}
        if file:
            kwargs["attachments"] = [file]
        await interaction.response.edit_message(**kwargs)
        await interaction.followup.send(f"Signed up for **{slot_name}**.", ephemeral=True)

    async def _set_rsvp(self, interaction: discord.Interaction, status: str):
        bind_guild(interaction.guild_id)
        with db_session() as session:
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
                record.slot_id = None  # tentative/decline never carry a role
                record.responded_at = datetime.utcnow()
            else:
                session.add(AttendanceRecord(event_id=self.event_id, member_id=interaction.user.id, status=status))

            member.last_active_date = datetime.utcnow()
            session.commit()

            embed, file = await self._rerender(session, event)

        kwargs = {"embed": embed, "view": self}
        if file:
            kwargs["attachments"] = [file]
        await interaction.response.edit_message(**kwargs)
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


_EVENT_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_EVENT_IMAGE_MAX_BYTES = 256 * 1024


async def _event_image_data_uri(attachment: discord.Attachment) -> tuple[str | None, str | None]:
    """Turn an attached image into a data URI, or (None, error) if it isn't a
    usable image -- mirrors the web app's upload validation (256 KB cap,
    PNG/JPEG/WebP/GIF only)."""
    import base64
    ctype = (attachment.content_type or "").split(";")[0].strip()
    if ctype not in _EVENT_IMAGE_TYPES:
        return None, "The image must be a PNG, JPEG, WebP, or GIF."
    if attachment.size > _EVENT_IMAGE_MAX_BYTES:
        return None, "The image must be under 256 KB."
    data = await attachment.read()
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}", None


def _event_color(color: str | None) -> tuple[int | None, str | None]:
    if not color:
        return None, None
    try:
        return int(color.strip().lstrip("#"), 16) & 0xFFFFFF, None
    except ValueError:
        return None, f"'{color}' isn't a valid hex colour, e.g. #7C1F2B. Using the unit's colour instead."


async def event_type_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=t, value=t) for t in EVENT_TYPES if current.lower() in t.lower()]


async def event_name_autocomplete(interaction: discord.Interaction, current: str):
    with db_session() as session:
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
    @app_commands.describe(description="Shown in the post body — loadout, meeting point, rules, etc.",
                           color="Accent colour as hex, e.g. #7C1F2B — blank uses the unit's own colour",
                           image="An image shown on the post")
    @is_officer()
    async def event_create(
        self,
        interaction: discord.Interaction,
        name: str,
        event_type: str,
        when: str,
        description: str | None = None,
        color: str | None = None,
        image: discord.Attachment | None = None,
    ):
        try:
            scheduled_at = datetime.strptime(when, "%Y-%m-%d %H:%M")
        except ValueError:
            return await interaction.response.send_message(
                "Invalid date format. Use `YYYY-MM-DD HH:MM` (UTC), e.g. `2026-07-01 19:00`.", ephemeral=True
            )

        warnings = []
        color_value, color_warn = _event_color(color)
        if color_warn:
            warnings.append(color_warn)
        image_uri = None
        if image is not None:
            image_uri, image_warn = await _event_image_data_uri(image)
            if image_warn:
                warnings.append(image_warn)

        with db_session() as session:
            announcements_channel_id = get_config(session).announcements_channel_id
        channel = (
            (interaction.guild.get_channel(announcements_channel_id) if announcements_channel_id else None)
            or interaction.channel
        )

        with db_session() as session:
            event = Event(
                name=name,
                event_type=event_type,
                scheduled_at=scheduled_at,
                created_by=interaction.user.id,
                channel_id=channel.id,
                description=(description or "").strip() or None,
                color=color_value,
                image=image_uri,
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            event_id = event.id
            buckets = _rsvp_buckets(session, event_id)
            embed, file = _build_event_embed(event, buckets)

        view = RSVPView(event_id)
        message = await channel.send(embed=embed, view=view, file=file) if file else \
            await channel.send(embed=embed, view=view)

        with db_session() as session:
            event = session.get(Event, event_id)
            event.message_id = message.id
            session.commit()

        reply = f"Event posted in {channel.mention}."
        if warnings:
            reply += " " + " ".join(warnings)
        await interaction.response.send_message(reply, ephemeral=True)

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

        with db_session() as session:
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

    @app_commands.command(name="event_roster", description="List everyone who has RSVPed to an event")
    @app_commands.autocomplete(event=event_name_autocomplete)
    async def event_roster(self, interaction: discord.Interaction, event: str):
        try:
            event_id = int(event)
        except ValueError:
            return await interaction.response.send_message("Select an event from the autocomplete list.", ephemeral=True)

        with db_session() as session:
            event_row = session.get(Event, event_id)
            if event_row is None:
                return await interaction.response.send_message("Event not found.", ephemeral=True)

            buckets = _rsvp_buckets(session, event_id)
            slots = (
                session.query(EventSlot).filter(EventSlot.event_id == event_id)
                .order_by(EventSlot.position).all()
            )
            counts = _slot_counts(session, event_id)
            embed, file = _build_event_embed(event_row, buckets, slots, counts)

        if file:
            await interaction.response.send_message(embed=embed, file=file)
        else:
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="attendance_history", description="View a member's attendance history")
    async def attendance_history(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        with db_session() as session:
            cfg = get_config(session)
        is_priv = any(r.id in (cfg.admin_role_id, cfg.officer_role_id) for r in interaction.user.roles)
        if target.id != interaction.user.id and not is_priv:
            return await interaction.response.send_message("You can only view your own attendance.", ephemeral=True)

        with db_session() as session:
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
                    lines.append(f"- {discord_ts(event.scheduled_at, 'd')} {event.name}: **{r.status}**")

        embed = base_embed(title=f"Attendance History: {target.display_name}")
        embed.description = "\n".join(lines) if lines else "No attendance records yet."
        await interaction.response.send_message(embed=embed, ephemeral=not is_priv)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
