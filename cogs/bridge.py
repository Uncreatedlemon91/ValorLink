"""The bot half of the web UI.

The website writes regiment data straight to the database and drops a
PendingAction on the queue for anything that has to happen inside Discord.
This cog drains that queue on a short loop and applies each action, reusing
the exact same sync helpers the slash commands use -- so a promotion issued
on the website swaps roles, rewrites the nickname, refreshes the roster and
dossier, and posts to the billboard just as `/promote` would.

Only the Discord side-effects live here; the data change already happened on
the web side and is the source of truth.
"""
import json
import logging
from datetime import datetime

import discord
from discord.ext import commands, tasks

import config
from db.base import SessionLocal
from db.models import Company, DisciplinaryRecord, Event, Member, PendingAction, Rank
from utils import queue
from utils.billboard import post_billboard
from utils.embeds import base_embed
from utils.settings import get_config
from utils.sync import sync_company, sync_rank

log = logging.getLogger("valorlink.bridge")

BATCH = 20
MAX_ATTEMPTS = 3
RECORD_COLORS = {
    "note": discord.Color.light_grey(),
    "warn": discord.Color.orange(),
    "strike": discord.Color.red(),
}


def _roster_refresh():
    from cogs.roster import refresh_roster
    return refresh_roster


def _personnel_refresh():
    from cogs.personnel import refresh_personnel_file
    return refresh_personnel_file


class Bridge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drain_queue.start()

    def cog_unload(self):
        self.drain_queue.cancel()

    @tasks.loop(seconds=4.0)
    async def drain_queue(self):
        if not config.GUILD_ID:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return

        with SessionLocal() as session:
            rows = (
                session.query(PendingAction)
                .filter(PendingAction.status == queue.PENDING)
                .order_by(PendingAction.id)
                .limit(BATCH)
                .all()
            )
            work = [(r.id, r.action, json.loads(r.payload or "{}")) for r in rows]

        for action_id, action, payload in work:
            try:
                await self._dispatch(guild, action, payload)
                self._finish(action_id, queue.DONE)
            except Exception as exc:  # noqa: BLE001 -- record and move on
                log.exception("Bridge action %s (%s) failed", action_id, action)
                self._finish(action_id, None, error=str(exc))

    @drain_queue.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    def _finish(self, action_id: int, status: str | None, error: str | None = None):
        with SessionLocal() as session:
            row = session.get(PendingAction, action_id)
            if row is None:
                return
            row.attempts += 1
            if status == queue.DONE:
                row.status = queue.DONE
                row.processed_at = datetime.utcnow()
                row.error = None
            else:
                row.error = error
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = queue.FAILED
                    row.processed_at = datetime.utcnow()
                # else: leave PENDING to retry on a later loop
            session.commit()

    # ------------------------------------------------------------------ #
    async def _dispatch(self, guild: discord.Guild, action: str, p: dict):
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            raise ValueError(f"Unknown bridge action: {action}")
        await handler(guild, p)

    # --- helpers ------------------------------------------------------- #
    def _thread_id(self, discord_id: int) -> int | None:
        with SessionLocal() as session:
            record = session.get(Member, discord_id)
            return record.thread_id if record else None

    async def _lock_thread(self, guild: discord.Guild, thread_id: int, locked: bool):
        if not thread_id:
            return
        thread = guild.get_channel_or_thread(thread_id)
        if thread is None:
            try:
                thread = await guild.fetch_channel(thread_id)
            except discord.HTTPException:
                return
        try:
            await thread.edit(archived=locked, locked=locked, reason="ValorLink web action")
        except discord.HTTPException:
            pass

    async def _log_embed(self, guild: discord.Guild, channel_attr: str, embed: discord.Embed):
        with SessionLocal() as session:
            channel_id = getattr(get_config(session), channel_attr)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    async def _refresh(self, guild: discord.Guild, discord_id: int, roster: bool = False):
        try:
            await _personnel_refresh()(guild, discord_id)
        except Exception:
            pass
        if roster:
            try:
                await _roster_refresh()(guild)
            except Exception:
                pass

    async def _dm(self, guild: discord.Guild, discord_id: int, text: str):
        member = guild.get_member(discord_id)
        if member and text:
            try:
                await member.send(text)
            except discord.HTTPException:
                pass

    # --- action handlers ---------------------------------------------- #
    async def _do_sync_rank(self, guild, p):
        member = guild.get_member(p["discord_id"])
        if member:
            await sync_rank(member, p["callsign"], p.get("old_rank"), p["new_rank"])
        await self._refresh(guild, p["discord_id"], roster=True)
        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_sync_company(self, guild, p):
        member = guild.get_member(p["discord_id"])
        if member:
            await sync_company(member, p.get("old_company"), p["new_company"])
        await self._refresh(guild, p["discord_id"], roster=True)
        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_refresh_personnel(self, guild, p):
        await self._refresh(guild, p["discord_id"])

    async def _do_discipline(self, guild, p):
        member = guild.get_member(p["discord_id"])
        record_type = p["record_type"]
        with SessionLocal() as session:
            strike_count = (
                session.query(DisciplinaryRecord)
                .filter(
                    DisciplinaryRecord.member_id == p["discord_id"],
                    DisciplinaryRecord.record_type == "strike",
                )
                .count()
            )
        embed = base_embed(
            title=f"Disciplinary {record_type.capitalize()}",
            color=RECORD_COLORS[record_type].value,
        )
        embed.add_field(name="Member", value=member.mention if member else str(p["discord_id"]), inline=True)
        embed.add_field(name="Issued By", value=f"<@{p['issued_by']}>", inline=True)
        embed.add_field(name="Reason", value=p["reason"], inline=False)
        if record_type == "strike":
            embed.add_field(name="Total Strikes", value=str(strike_count), inline=True)
        await self._log_embed(guild, "mod_log_channel_id", embed)
        await self._dm(guild, p["discord_id"], p.get("dm", ""))
        await self._refresh(guild, p["discord_id"])

    async def _do_discharge(self, guild, p):
        member = guild.get_member(p["discord_id"])
        if member:
            await self._strip_managed_roles(member)
        await self._refresh(guild, p["discord_id"], roster=True)
        await self._lock_thread(guild, self._thread_id(p["discord_id"]), locked=True)

        embed = base_embed(
            title=f"{p['verb']} Discharge",
            color=(discord.Color.green().value if p["verb"] == "Honorably" else discord.Color.red().value),
        )
        embed.add_field(name="Member", value=member.mention if member else str(p["discord_id"]), inline=True)
        embed.add_field(name="Rank at Discharge", value=p.get("rank_at_discharge", "—"), inline=True)
        embed.add_field(name="Discharged By", value=f"<@{p['actor_id']}>", inline=True)
        embed.add_field(name="Reason", value=p["reason"], inline=False)
        await self._log_embed(guild, "admin_log_channel_id", embed)

        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_reinstate(self, guild, p):
        member = guild.get_member(p["discord_id"])
        with SessionLocal() as session:
            member_role_id = get_config(session).member_role_id
        if member and member_role_id:
            role = guild.get_role(member_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Reinstated via web")
                except discord.HTTPException:
                    pass
        await self._lock_thread(guild, self._thread_id(p["discord_id"]), locked=False)
        await self._refresh(guild, p["discord_id"], roster=True)

        embed = base_embed(title="Member Reinstated", color=discord.Color.green().value)
        embed.add_field(name="Member", value=member.mention if member else str(p["discord_id"]), inline=True)
        embed.add_field(name="Reinstated By", value=f"<@{p['actor_id']}>", inline=True)
        if p.get("reason"):
            embed.add_field(name="Reason", value=p["reason"], inline=False)
        await self._log_embed(guild, "admin_log_channel_id", embed)

        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_loa(self, guild, p):
        await self._dm(guild, p["discord_id"], p.get("dm", ""))
        await self._refresh(guild, p["discord_id"])
        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_loa_end(self, guild, p):
        await self._refresh(guild, p["discord_id"])
        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_approve_candidate(self, guild, p):
        applicant = guild.get_member(p["discord_id"])
        with SessionLocal() as session:
            cfg = get_config(session)
            candidate_role_id = cfg.candidate_role_id
            member_role_id = cfg.member_role_id
            personnel_forum_id = cfg.personnel_forum_id

        if applicant:
            try:
                if candidate_role_id:
                    role = guild.get_role(candidate_role_id)
                    if role:
                        await applicant.remove_roles(role)
                if member_role_id:
                    role = guild.get_role(member_role_id)
                    if role:
                        await applicant.add_roles(role)
            except discord.HTTPException:
                pass

        # Open the dossier thread and record its id back on the member.
        thread_id = None
        forum = guild.get_channel(personnel_forum_id) if personnel_forum_id else None
        if isinstance(forum, discord.ForumChannel):
            try:
                created = await forum.create_thread(
                    name=f"{p['callsign']}",
                    content=f"**Personnel Dossier: {p['callsign']}**\nEnlisted via the regiment website.",
                )
                thread_id = created.thread.id
            except discord.HTTPException:
                thread_id = None
        if thread_id:
            with SessionLocal() as session:
                record = session.get(Member, p["discord_id"])
                if record:
                    record.thread_id = thread_id
                    session.commit()

        if applicant:
            await sync_rank(applicant, p["callsign"], None, p["default_rank"])
            await sync_company(applicant, None, p["default_company"])

        await self._refresh(guild, p["discord_id"], roster=True)

        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

        embed = base_embed(title="Enlistment Approved", color=discord.Color.green().value)
        embed.add_field(name="Applicant", value=applicant.mention if applicant else str(p["discord_id"]))
        embed.add_field(name="Approved By", value=f"<@{p['actor_id']}>")
        embed.set_footer(text=f"ID: {p['discord_id']}")
        await self._log_embed(guild, "admin_log_channel_id", embed)

        await self._dm(guild, p["discord_id"], p.get("dm", ""))

    async def _do_deny_candidate(self, guild, p):
        applicant = guild.get_member(p["discord_id"])
        embed = base_embed(title="Enlistment Denied", color=discord.Color.red().value)
        embed.add_field(name="Applicant", value=applicant.mention if applicant else str(p["discord_id"]))
        embed.add_field(name="Denied By", value=f"<@{p['actor_id']}>")
        await self._log_embed(guild, "admin_log_channel_id", embed)
        await self._dm(guild, p["discord_id"], p.get("dm", ""))

    async def _do_announce_event(self, guild, p):
        from cogs.events import RSVPView, _build_event_embed, _rsvp_buckets

        with SessionLocal() as session:
            event = session.get(Event, p["event_id"])
            if event is None:
                return
            channel_id = get_config(session).announcements_channel_id
            buckets = _rsvp_buckets(session, event.id)
            embed = _build_event_embed(event, buckets)
            event_id = event.id

        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return  # no announcements channel configured; the event still lives on the site

        view = RSVPView(event_id)
        message = await channel.send(embed=embed, view=view)

        with SessionLocal() as session:
            row = session.get(Event, event_id)
            if row:
                row.message_id = message.id
                row.channel_id = channel.id
                session.commit()

        self.bot.add_view(view, message_id=message.id)

    async def _do_award_granted(self, guild, p):
        await self._refresh(guild, p["discord_id"])
        if p.get("billboard"):
            await post_billboard(guild, p["billboard"])

    async def _do_award_revoked(self, guild, p):
        await self._refresh(guild, p["discord_id"])

    async def _strip_managed_roles(self, member: discord.Member):
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
                await member.remove_roles(*to_remove, reason="Discharge via web")
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Bridge(bot))
