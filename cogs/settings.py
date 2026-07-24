import discord
from discord import app_commands
from discord.ext import commands

from db.base import db_session
from db.models import Company, Member, Rank
from utils.checks import is_bot_admin, is_officer
from utils.embeds import base_embed
from utils.settings import CHANNEL_KEYS, ROLE_KEYS, get_config
from utils.sync import resync_nickname


async def role_key_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=key, value=key) for key in ROLE_KEYS if current.lower() in key.lower()
    ]


async def channel_key_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=key, value=key) for key in CHANNEL_KEYS if current.lower() in key.lower()
    ]


async def rank_name_autocomplete(interaction: discord.Interaction, current: str):
    with db_session() as session:
        names = [r.name for r in session.query(Rank).order_by(Rank.position).all()]
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


async def company_name_autocomplete(interaction: discord.Interaction, current: str):
    with db_session() as session:
        names = [c.name for c in session.query(Company).order_by(Company.name).all()]
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config_group = app_commands.Group(name="config", description="Configure ValorLink for this server")
    rank_group = app_commands.Group(name="rank", description="Manage the rank ladder")
    company_group = app_commands.Group(name="company", description="Manage companies")

    # --- /config ---

    @config_group.command(name="set_role", description="Bind a regiment role to a ValorLink permission tier")
    @app_commands.autocomplete(key=role_key_autocomplete)
    @is_bot_admin()
    async def config_set_role(self, interaction: discord.Interaction, key: str, role: discord.Role):
        if key not in ROLE_KEYS:
            return await interaction.response.send_message(
                f"Unknown role key `{key}`. Valid keys: {', '.join(ROLE_KEYS)}", ephemeral=True
            )
        with db_session() as session:
            cfg = get_config(session)
            setattr(cfg, ROLE_KEYS[key], role.id)
            session.commit()
        await interaction.response.send_message(f"`{key}` role set to {role.mention}.", ephemeral=True)

    @config_group.command(name="set_channel", description="Bind a channel to a ValorLink function")
    @app_commands.autocomplete(key=channel_key_autocomplete)
    @is_bot_admin()
    async def config_set_channel(
        self,
        interaction: discord.Interaction,
        key: str,
        channel: discord.TextChannel | discord.ForumChannel,
    ):
        if key not in CHANNEL_KEYS:
            return await interaction.response.send_message(
                f"Unknown channel key `{key}`. Valid keys: {', '.join(CHANNEL_KEYS)}", ephemeral=True
            )
        with db_session() as session:
            cfg = get_config(session)
            setattr(cfg, CHANNEL_KEYS[key], channel.id)
            session.commit()
        await interaction.response.send_message(f"`{key}` channel set to {channel.mention}.", ephemeral=True)

    @config_group.command(name="set_name", description="Set the regiment's display name")
    @is_bot_admin()
    async def config_set_name(self, interaction: discord.Interaction, name: str):
        with db_session() as session:
            cfg = get_config(session)
            cfg.regiment_name = name
            session.commit()
        await interaction.response.send_message(f"Regiment name set to **{name}**.", ephemeral=True)

    @config_group.command(name="set_motto", description="Set the regiment's motto (shown in embed footers)")
    @is_bot_admin()
    async def config_set_motto(self, interaction: discord.Interaction, motto: str):
        with db_session() as session:
            cfg = get_config(session)
            cfg.regiment_motto = motto
            session.commit()
        await interaction.response.send_message(f"Regiment motto set to **{motto}**.", ephemeral=True)

    @config_group.command(name="set_unit_tag", description="Set the tag prefixed onto every member's nickname (blank to clear)")
    @is_bot_admin()
    async def config_set_unit_tag(self, interaction: discord.Interaction, tag: str = ""):
        tag = tag.strip()
        if len(tag) > 16:
            return await interaction.response.send_message(
                "The unit tag must be 16 characters or fewer.", ephemeral=True)
        with db_session() as session:
            get_config(session).unit_tag = tag or None
            member_ids = [row[0] for row in session.query(Member.discord_id).filter(
                Member.status != "discharged")]
            session.commit()
        await interaction.response.send_message(
            (f"Unit tag set to **{tag}**." if tag else "Unit tag cleared.")
            + f" Rebuilding {len(member_ids)} nickname(s)…", ephemeral=True)
        await self._rebuild_nicknames(interaction.guild, member_ids)

    async def _rebuild_nicknames(self, guild: discord.Guild, member_ids: list[int]):
        for discord_id in member_ids:
            member = guild.get_member(discord_id)
            if member:
                await resync_nickname(member)

    @config_group.command(name="set_color", description="Set the brand color used in embeds (hex, e.g. #2F3136)")
    @is_bot_admin()
    async def config_set_color(self, interaction: discord.Interaction, hex_color: str):
        try:
            value = int(hex_color.lstrip("#"), 16)
        except ValueError:
            return await interaction.response.send_message(
                "Invalid hex color. Use a format like `#2F3136`.", ephemeral=True
            )
        with db_session() as session:
            cfg = get_config(session)
            cfg.brand_color = value
            session.commit()
        await interaction.response.send_message(f"Brand color set to `#{value:06X}`.", ephemeral=True)

    @config_group.command(name="set_inactivity_days", description="Days of inactivity before a member is auto-flagged")
    @is_bot_admin()
    async def config_set_inactivity_days(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 365]):
        with db_session() as session:
            cfg = get_config(session)
            cfg.inactivity_days_threshold = days
            session.commit()
        await interaction.response.send_message(f"Inactivity threshold set to **{days}** day(s).", ephemeral=True)

    @config_group.command(name="show", description="View ValorLink's current configuration")
    @is_officer()
    async def config_show(self, interaction: discord.Interaction):
        with db_session() as session:
            cfg = get_config(session)
            embed = base_embed(title=f"{cfg.regiment_name} -- ValorLink Configuration")
            embed.add_field(name="Motto", value=cfg.regiment_motto or "*not set*", inline=False)
            embed.add_field(name="Brand Color", value=f"#{cfg.brand_color:06X}", inline=True)
            embed.add_field(name="Inactivity Threshold", value=f"{cfg.inactivity_days_threshold} day(s)", inline=True)

            roles_text = "\n".join(
                f"**{key}**: " + (f"<@&{getattr(cfg, col)}>" if getattr(cfg, col) else "*not set*")
                for key, col in ROLE_KEYS.items()
            )
            embed.add_field(name="Roles", value=roles_text, inline=False)

            channels_text = "\n".join(
                f"**{key}**: " + (f"<#{getattr(cfg, col)}>" if getattr(cfg, col) else "*not set*")
                for key, col in CHANNEL_KEYS.items()
            )
            embed.add_field(name="Channels", value=channels_text, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /rank ---

    @rank_group.command(name="add", description="Add a new rank to the top of the ladder")
    @is_bot_admin()
    async def rank_add(
        self,
        interaction: discord.Interaction,
        name: str,
        abbreviation: str,
        tier: str | None = None,
        role: discord.Role | None = None,
    ):
        with db_session() as session:
            if session.query(Rank).filter(Rank.name == name).one_or_none():
                return await interaction.response.send_message(f"Rank **{name}** already exists.", ephemeral=True)
            top = session.query(Rank).order_by(Rank.position.desc()).first()
            position = (top.position + 1) if top else 0
            session.add(
                Rank(
                    name=name,
                    abbreviation=abbreviation,
                    tier=tier,
                    role_id=role.id if role else None,
                    position=position,
                )
            )
            session.commit()
        await interaction.response.send_message(f"Rank **{name}** added at the top of the ladder.", ephemeral=True)

    @rank_group.command(name="remove", description="Remove a rank from the ladder")
    @app_commands.autocomplete(name=rank_name_autocomplete)
    @is_bot_admin()
    async def rank_remove(self, interaction: discord.Interaction, name: str):
        with db_session() as session:
            record = session.query(Rank).filter(Rank.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown rank: {name}", ephemeral=True)
            holders = session.query(Member).filter(Member.rank == name).count()
            if holders:
                return await interaction.response.send_message(
                    f"**{name}** is still held by {holders} member(s). Reassign them to a different "
                    "rank before removing it, or their Discord nickname tag will fall out of sync "
                    "with the roster.", ephemeral=True
                )
            session.delete(record)
            session.commit()
        await interaction.response.send_message(f"Rank **{name}** removed.", ephemeral=True)

    @rank_group.command(name="set_role", description="Bind (or clear) a rank's synced Discord role")
    @app_commands.autocomplete(name=rank_name_autocomplete)
    @is_bot_admin()
    async def rank_set_role(self, interaction: discord.Interaction, name: str, role: discord.Role | None = None):
        with db_session() as session:
            record = session.query(Rank).filter(Rank.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown rank: {name}", ephemeral=True)
            record.role_id = role.id if role else None
            session.commit()
        await interaction.response.send_message(
            f"Rank **{name}** role set to {role.mention if role else '*none*'}.", ephemeral=True
        )

    @rank_group.command(name="move", description="Move a rank up or down the ladder")
    @app_commands.autocomplete(name=rank_name_autocomplete)
    @app_commands.choices(
        direction=[app_commands.Choice(name="up", value="up"), app_commands.Choice(name="down", value="down")]
    )
    @is_bot_admin()
    async def rank_move(self, interaction: discord.Interaction, name: str, direction: app_commands.Choice[str]):
        with db_session() as session:
            record = session.query(Rank).filter(Rank.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown rank: {name}", ephemeral=True)

            if direction.value == "up":
                neighbor = (
                    session.query(Rank)
                    .filter(Rank.position > record.position)
                    .order_by(Rank.position)
                    .first()
                )
            else:
                neighbor = (
                    session.query(Rank)
                    .filter(Rank.position < record.position)
                    .order_by(Rank.position.desc())
                    .first()
                )

            if neighbor is None:
                return await interaction.response.send_message(
                    f"**{name}** is already at the {'top' if direction.value == 'up' else 'bottom'}.", ephemeral=True
                )

            record.position, neighbor.position = neighbor.position, record.position
            session.commit()
        await interaction.response.send_message(f"Moved **{name}** {direction.value}.", ephemeral=True)

    @rank_group.command(name="list", description="View the rank ladder")
    async def rank_list(self, interaction: discord.Interaction):
        with db_session() as session:
            ranks = session.query(Rank).order_by(Rank.position).all()
            embed = base_embed(title="Rank Ladder")
            if not ranks:
                embed.description = "No ranks configured yet. Add one with `/rank add`."
            else:
                lines = []
                for r in ranks:
                    role_text = f" -- <@&{r.role_id}>" if r.role_id else ""
                    tier_text = f" [{r.tier}]" if r.tier else ""
                    lines.append(f"**{r.abbreviation}** {r.name}{tier_text}{role_text}")
                embed.description = "\n".join(reversed(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /company ---

    @company_group.command(name="add", description="Add a new company")
    @is_bot_admin()
    async def company_add(
        self,
        interaction: discord.Interaction,
        name: str,
        role: discord.Role | None = None,
        is_default: bool = False,
        tag: str = "",
    ):
        tag = tag.strip()
        if len(tag) > 16:
            return await interaction.response.send_message(
                "The company tag must be 16 characters or fewer.", ephemeral=True)
        with db_session() as session:
            if session.query(Company).filter(Company.name == name).one_or_none():
                return await interaction.response.send_message(f"Company **{name}** already exists.", ephemeral=True)
            if is_default:
                session.query(Company).update({Company.is_default: False})
            session.add(Company(name=name, role_id=role.id if role else None,
                                is_default=is_default, tag=tag or None))
            session.commit()
        await interaction.response.send_message(f"Company **{name}** added.", ephemeral=True)

    @company_group.command(name="remove", description="Remove a company")
    @app_commands.autocomplete(name=company_name_autocomplete)
    @is_bot_admin()
    async def company_remove(self, interaction: discord.Interaction, name: str):
        with db_session() as session:
            record = session.query(Company).filter(Company.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown company: {name}", ephemeral=True)
            holders = session.query(Member).filter(Member.company == name).count()
            if holders:
                return await interaction.response.send_message(
                    f"**{name}** still has {holders} member(s) assigned. Reassign them to a different "
                    "company before removing it, or their Discord nickname tag will fall out of sync "
                    "with the roster.", ephemeral=True
                )
            session.delete(record)
            session.commit()
        await interaction.response.send_message(f"Company **{name}** removed.", ephemeral=True)

    @company_group.command(name="set_role", description="Bind (or clear) a company's synced Discord role")
    @app_commands.autocomplete(name=company_name_autocomplete)
    @is_bot_admin()
    async def company_set_role(self, interaction: discord.Interaction, name: str, role: discord.Role | None = None):
        with db_session() as session:
            record = session.query(Company).filter(Company.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown company: {name}", ephemeral=True)
            record.role_id = role.id if role else None
            session.commit()
        await interaction.response.send_message(
            f"Company **{name}** role set to {role.mention if role else '*none*'}.", ephemeral=True
        )

    @company_group.command(name="set_tag", description="Set (or clear) a company's nickname tag")
    @app_commands.autocomplete(name=company_name_autocomplete)
    @is_bot_admin()
    async def company_set_tag(self, interaction: discord.Interaction, name: str, tag: str = ""):
        tag = tag.strip()
        if len(tag) > 16:
            return await interaction.response.send_message(
                "The company tag must be 16 characters or fewer.", ephemeral=True)
        with db_session() as session:
            record = session.query(Company).filter(Company.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown company: {name}", ephemeral=True)
            record.tag = tag or None
            member_ids = [row[0] for row in session.query(Member.discord_id).filter(
                Member.company == name, Member.status != "discharged")]
            session.commit()
        await interaction.response.send_message(
            (f"Company **{name}** tag set to **{tag}**." if tag else f"Company **{name}** tag cleared.")
            + f" Rebuilding {len(member_ids)} nickname(s)…", ephemeral=True)
        await self._rebuild_nicknames(interaction.guild, member_ids)

    @company_group.command(name="set_default", description="Set the company assigned to new enlistees")
    @app_commands.autocomplete(name=company_name_autocomplete)
    @is_bot_admin()
    async def company_set_default(self, interaction: discord.Interaction, name: str):
        with db_session() as session:
            record = session.query(Company).filter(Company.name == name).one_or_none()
            if record is None:
                return await interaction.response.send_message(f"Unknown company: {name}", ephemeral=True)
            session.query(Company).update({Company.is_default: False})
            record.is_default = True
            session.commit()
        await interaction.response.send_message(f"**{name}** is now the default company for new enlistees.", ephemeral=True)

    @company_group.command(name="list", description="View configured companies")
    async def company_list(self, interaction: discord.Interaction):
        with db_session() as session:
            companies = session.query(Company).order_by(Company.name).all()
            embed = base_embed(title="Companies")
            if not companies:
                embed.description = "No companies configured yet. Add one with `/company add`."
            else:
                lines = []
                for c in companies:
                    role_text = f" -- <@&{c.role_id}>" if c.role_id else ""
                    default_text = " *(default)*" if c.is_default else ""
                    lines.append(f"**{c.name}**{default_text}{role_text}")
                embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
