import discord
from discord import app_commands
from discord.ext import commands

from db.base import db_session
from db.models import AwardType, Member, MemberAward
from utils.billboard import post_billboard
from utils.checks import is_officer
from utils.embeds import base_embed


async def award_type_autocomplete(interaction: discord.Interaction, current: str):
    with db_session() as session:
        types = session.query(AwardType).filter(AwardType.name.ilike(f"%{current}%")).limit(25).all()
        return [app_commands.Choice(name=t.name, value=t.name) for t in types]


class Awards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="award_type_create", description="Add a new award/qualification to the catalog")
    @is_officer()
    async def award_type_create(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str = "",
        emoji: str = "",
    ):
        with db_session() as session:
            existing = session.query(AwardType).filter(AwardType.name.ilike(name)).one_or_none()
            if existing:
                return await interaction.response.send_message(f"`{name}` already exists in the catalog.", ephemeral=True)

            session.add(
                AwardType(
                    name=name,
                    description=description or None,
                    emoji=emoji or None,
                    created_by=interaction.user.id,
                )
            )
            session.commit()

        await interaction.response.send_message(f"Added **{name}** to the award/qualification catalog.")

    @app_commands.command(name="award_type_list", description="List the award/qualification catalog")
    async def award_type_list(self, interaction: discord.Interaction):
        with db_session() as session:
            types = session.query(AwardType).order_by(AwardType.name).all()

        embed = base_embed(title="Award / Qualification Catalog")
        if not types:
            embed.description = "No award types yet. Officers can add one with /award_type_create."
        else:
            embed.description = "\n".join(
                f"{(t.emoji + ' ') if t.emoji else ''}**{t.name}** - {t.description or 'No description'}"
                for t in types
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="award", description="Grant an award/qualification to a member")
    @app_commands.autocomplete(award_type=award_type_autocomplete)
    @is_officer()
    async def award(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        award_type: str,
        notes: str = "",
    ):
        with db_session() as session:
            target = session.get(Member, member.id)
            if target is None:
                return await interaction.response.send_message("That member has no personnel record.", ephemeral=True)

            award_row = session.query(AwardType).filter(AwardType.name.ilike(award_type)).one_or_none()
            if award_row is None:
                return await interaction.response.send_message(
                    f"`{award_type}` isn't in the catalog. Add it first with /award_type_create.", ephemeral=True
                )

            existing = (
                session.query(MemberAward)
                .filter(MemberAward.member_id == member.id, MemberAward.award_type_id == award_row.id)
                .one_or_none()
            )
            if existing:
                return await interaction.response.send_message(
                    f"{member.mention} already holds **{award_row.name}**.", ephemeral=True
                )

            session.add(
                MemberAward(
                    member_id=member.id,
                    award_type_id=award_row.id,
                    awarded_by=interaction.user.id,
                    notes=notes or None,
                )
            )
            session.commit()
            callsign = target.callsign
            label = f"{(award_row.emoji + ' ') if award_row.emoji else ''}{award_row.name}"

        await interaction.response.send_message(f"{member.mention} was awarded **{label}**.")

        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass

        await post_billboard(interaction.guild, f"**{callsign}** has been awarded **{award_row.name}**.")

    @app_commands.command(name="award_remove", description="Revoke an award/qualification from a member")
    @app_commands.autocomplete(award_type=award_type_autocomplete)
    @is_officer()
    async def award_remove(self, interaction: discord.Interaction, member: discord.Member, award_type: str):
        with db_session() as session:
            award_row = session.query(AwardType).filter(AwardType.name.ilike(award_type)).one_or_none()
            if award_row is None:
                return await interaction.response.send_message(f"`{award_type}` isn't in the catalog.", ephemeral=True)

            existing = (
                session.query(MemberAward)
                .filter(MemberAward.member_id == member.id, MemberAward.award_type_id == award_row.id)
                .one_or_none()
            )
            if existing is None:
                return await interaction.response.send_message(
                    f"{member.mention} doesn't hold **{award_row.name}**.", ephemeral=True
                )

            session.delete(existing)
            session.commit()

        await interaction.response.send_message(f"Removed **{award_row.name}** from {member.mention}.")

        try:
            from cogs.personnel import refresh_personnel_file
            await refresh_personnel_file(interaction.guild, member.id)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Awards(bot))
