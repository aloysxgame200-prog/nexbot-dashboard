"""Suggestions (votes + validation du staff) et signalements privés au staff."""
from __future__ import annotations

import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from ...core.db import session_scope
from ...core.models import Report, Suggestion
from .. import utils
from ..client import Nexbot

log = logging.getLogger("nexbot.community")
SUGG_STATUS = {"pending": ("⏳ En attente", utils.WARNING), "accepted": ("✅ Acceptée", utils.SUCCESS),
               "refused": ("❌ Refusée", utils.DANGER)}
REPORT_STATUS = {"open": "🟠 Ouvert", "handled": "🟢 Traité", "dismissed": "⚪ Classé"}


class SuggestionButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"nexbot:sugg:(?P<action>accept|refuse):(?P<id>\d+)"):
    def __init__(self, action: str, suggestion_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Accepter" if action == "accept" else "Refuser",
            style=discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.danger,
            custom_id=f"nexbot:sugg:{action}:{suggestion_id}"))
        self.action, self.suggestion_id = action, suggestion_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Community = interaction.client.get_cog("Community")  # type: ignore[assignment,union-attr]
        await cog.review_suggestion(interaction, self.suggestion_id, self.action == "accept")


class ReportButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"nexbot:report:(?P<status>handled|dismissed):(?P<id>\d+)"):
    def __init__(self, status: str, report_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Traité" if status == "handled" else "Classer sans suite",
            style=discord.ButtonStyle.success if status == "handled" else discord.ButtonStyle.secondary,
            custom_id=f"nexbot:report:{status}:{report_id}"))
        self.status, self.report_id = status, report_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["status"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Community = interaction.client.get_cog("Community")  # type: ignore[assignment,union-attr]
        await cog.update_report(interaction, self.report_id, self.status)


class ReviewModal(discord.ui.Modal):
    comment = discord.ui.TextInput(label="Commentaire (facultatif)", style=discord.TextStyle.paragraph,
                                   required=False, max_length=500)

    def __init__(self, cog: "Community", suggestion_id: int, accepted: bool) -> None:
        super().__init__(title="Accepter la suggestion" if accepted else "Refuser la suggestion")
        self.cog, self.suggestion_id, self.accepted = cog, suggestion_id, accepted

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_review(interaction, self.suggestion_id, self.accepted, str(self.comment.value or ""))


class ReportModal(discord.ui.Modal, title="Signaler à la modération"):
    reason = discord.ui.TextInput(label="Pourquoi signalez-vous ce message ?", style=discord.TextStyle.paragraph,
                                  min_length=3, max_length=800)

    def __init__(self, cog: "Community", message: discord.Message) -> None:
        super().__init__()
        self.cog, self.message = cog, message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_report(interaction, self.message.author, str(self.reason.value), self.message.jump_url,
                                     utils.truncate(self.message.content, 500))


class Community(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot
        self._report_cooldown: dict[int, float] = {}
        self.report_menu = app_commands.ContextMenu(name="Signaler le message", callback=self.report_message)
        self.report_menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(SuggestionButton, ReportButton)
        self.bot.tree.add_command(self.report_menu)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(SuggestionButton, ReportButton)
        self.bot.tree.remove_command(self.report_menu.name, type=self.report_menu.type)

    # ================================================================== suggestions
    @app_commands.command(name="suggest", description="Proposer une suggestion au serveur")
    @app_commands.rename(content="suggestion")
    @app_commands.guild_only()
    async def suggest(self, interaction: discord.Interaction, content: app_commands.Range[str, 5, 1500]) -> None:
        guild = interaction.guild
        assert guild is not None
        conf = await self.bot.conf(guild.id, "suggestions")
        channel = guild.get_channel(utils.snowflake(conf.channel_id) or 0)
        if not (conf.enabled and self.bot.globally_enabled("suggestions")) or not isinstance(channel, discord.TextChannel):
            await utils.reply(interaction, utils.err("Les suggestions ne sont pas activées sur ce serveur."))
            return

        def create():
            with session_scope() as db:
                s = Suggestion(guild_id=guild.id, author_id=interaction.user.id, content=content, channel_id=channel.id)
                db.add(s)
                db.flush()
                return s.id
        sid = await self.bot.db(create)
        embed = discord.Embed(title=f"💡 Suggestion n°{sid}", description=content, color=SUGG_STATUS["pending"][1])
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Statut", value=SUGG_STATUS["pending"][0])
        view = discord.ui.View(timeout=None)
        view.add_item(SuggestionButton("accept", sid))
        view.add_item(SuggestionButton("refuse", sid))
        try:
            msg = await channel.send(embed=embed, view=view)
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")
        except discord.HTTPException:
            await utils.reply(interaction, utils.err("Je ne peux pas écrire dans le salon des suggestions."))
            return
        if conf.create_threads:
            try:
                await msg.create_thread(name=f"Suggestion n°{sid}", auto_archive_duration=10080)
            except discord.HTTPException:
                pass

        def set_msg():
            with session_scope() as db:
                row = db.get(Suggestion, sid)
                if row:
                    row.message_id = msg.id
        await self.bot.db(set_msg)
        await utils.reply(interaction, utils.ok(f"Suggestion publiée dans {channel.mention}. Merci !"))

    async def review_suggestion(self, interaction: discord.Interaction, sid: int, accepted: bool) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        conf = await self.bot.conf(member.guild.id, "suggestions")
        if not (member.guild_permissions.manage_guild or any(str(r.id) in conf.reviewer_role_ids for r in member.roles)):
            await utils.reply(interaction, utils.err("Seul le staff peut valider les suggestions. Votez avec 👍 / 👎 !"))
            return
        await interaction.response.send_modal(ReviewModal(self, sid, accepted))

    async def apply_review(self, interaction: discord.Interaction, sid: int, accepted: bool, comment: str) -> None:
        status = "accepted" if accepted else "refused"

        def save():
            with session_scope() as db:
                row = db.get(Suggestion, sid)
                if row is None or row.guild_id != interaction.guild_id:
                    return None
                row.status, row.staff_comment = status, comment
                return row.author_id
        author_id = await self.bot.db(save)
        if author_id is None:
            await utils.reply(interaction, utils.err("Suggestion introuvable."))
            return
        message = interaction.message
        if message and message.embeds:
            embed = message.embeds[0]
            embed.color = discord.Color(SUGG_STATUS[status][1])
            embed.clear_fields()
            embed.add_field(name="Statut", value=f"{SUGG_STATUS[status][0]} par {interaction.user.mention}")
            if comment:
                embed.add_field(name="Commentaire du staff", value=comment, inline=False)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await utils.reply(interaction, utils.ok("Suggestion mise à jour."))
        author = interaction.guild.get_member(author_id) if interaction.guild else None
        if author:
            try:
                await author.send(f"Votre suggestion n°{sid} sur **{interaction.guild.name}** a été "  # type: ignore[union-attr]
                                  f"{'acceptée ✅' if accepted else 'refusée ❌'}." + (f"\n> {comment}" if comment else ""))
            except discord.HTTPException:
                pass

    # ================================================================== signalements
    @app_commands.command(name="report", description="Signaler un membre au staff (privé)")
    @app_commands.rename(member="membre", reason="raison", link="lien_message")
    @app_commands.describe(link="Lien vers le message concerné (facultatif)")
    @app_commands.guild_only()
    async def report(self, interaction: discord.Interaction, member: discord.Member,
                     reason: app_commands.Range[str, 3, 800], link: str = "") -> None:
        if link and not re.match(r"^https://(ptb\.|canary\.)?discord(app)?\.com/channels/\d+/\d+/\d+$", link):
            await utils.reply(interaction, utils.err("Lien de message invalide."))
            return
        await self.create_report(interaction, member, reason, link, "")

    async def report_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.send_modal(ReportModal(self, message))

    async def create_report(self, interaction: discord.Interaction, target: discord.abc.User, reason: str,
                            link: str, excerpt: str) -> None:
        guild = interaction.guild
        assert guild is not None
        conf = await self.bot.conf(guild.id, "reports")
        channel = guild.get_channel(utils.snowflake(conf.channel_id) or 0)
        if not (conf.enabled and self.bot.globally_enabled("reports")) or not isinstance(channel, discord.TextChannel):
            await utils.reply(interaction, utils.err("Les signalements ne sont pas activés sur ce serveur."))
            return
        if target.id == interaction.user.id or target.bot:
            await utils.reply(interaction, utils.err("Vous ne pouvez pas signaler ce compte."))
            return
        now = time.monotonic()
        if now - self._report_cooldown.get(interaction.user.id, 0) < 60:
            await utils.reply(interaction, utils.err("Patientez une minute entre deux signalements."))
            return
        self._report_cooldown[interaction.user.id] = now

        def create():
            with session_scope() as db:
                r = Report(guild_id=guild.id, reporter_id=interaction.user.id, target_id=target.id,
                           reason=reason, message_link=link[:200])
                db.add(r)
                db.flush()
                return r.id
        rid = await self.bot.db(create)
        embed = discord.Embed(title=f"🚩 Signalement n°{rid}", color=utils.WARNING)
        embed.add_field(name="Membre signalé", value=f"{target.mention} (`{target.id}`)")
        embed.add_field(name="Par", value=interaction.user.mention)
        embed.add_field(name="Statut", value=REPORT_STATUS["open"])
        embed.add_field(name="Raison", value=reason, inline=False)
        if excerpt:
            embed.add_field(name="Message", value=excerpt, inline=False)
        if link:
            embed.add_field(name="Lien", value=f"[Voir le message]({link})", inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(ReportButton("handled", rid))
        view.add_item(ReportButton("dismissed", rid))
        ping = " ".join(f"<@&{r}>" for r in conf.staff_role_ids) if conf.ping_staff else None
        try:
            await channel.send(content=ping or None, embed=embed, view=view,
                               allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            await utils.reply(interaction, utils.err("Le salon des signalements est inaccessible pour Nexbot."))
            return
        self.bot.record_usage(interaction.user.id, "report")
        await utils.reply(interaction, utils.ok("Merci, votre signalement a été transmis au staff en privé."))

    async def update_report(self, interaction: discord.Interaction, rid: int, status: str) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        conf = await self.bot.conf(member.guild.id, "reports")
        if not (await self.bot.is_moderator(member, "moderate_members")
                or any(str(r.id) in conf.staff_role_ids for r in member.roles)):
            await utils.reply(interaction, utils.err("Réservé au staff."))
            return

        def save():
            with session_scope() as db:
                row = db.get(Report, rid)
                if row is None or row.guild_id != member.guild.id:
                    return False
                row.status, row.handled_by = status, member.id
                return True
        if not await self.bot.db(save):
            await utils.reply(interaction, utils.err("Signalement introuvable."))
            return
        message = interaction.message
        if message and message.embeds:
            embed = message.embeds[0]
            for i, f in enumerate(embed.fields):
                if f.name == "Statut":
                    embed.set_field_at(i, name="Statut", value=f"{REPORT_STATUS[status]} par {member.mention}")
            embed.color = discord.Color(utils.SUCCESS if status == "handled" else 0x99AAB5)
            await interaction.response.edit_message(embed=embed, view=None)


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Community(bot))
