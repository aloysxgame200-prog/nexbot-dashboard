"""Tickets : panneau, ouverture privée, fermeture avec transcription, catégories et fermeture auto (Premium)."""
from __future__ import annotations

import io
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from ...core.db import session_scope
from ...core.embeds import render_text
from ...core.models import Ticket, utcnow
from .. import utils
from ..client import Nexbot

log = logging.getLogger("nexbot.tickets")

OPEN_ID = "nexbot:ticket:open"
SELECT_ID = "nexbot:ticket:select"
CLOSE_ID = "nexbot:ticket:close"
STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
          "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}


class PanelView(discord.ui.View):
    """Vue persistante : reste fonctionnelle après un redémarrage du bot."""

    def __init__(self, cog: "Tickets", label: str = "Ouvrir un ticket", style: str = "primary",
                 categories: list[str] | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if categories:
            select = discord.ui.Select(custom_id=SELECT_ID, placeholder="Choisissez le motif de votre demande",
                                       options=[discord.SelectOption(label=c[:100], value=c[:100]) for c in categories[:25]])
            select.callback = self._select
            self.add_item(select)
        else:
            button = discord.ui.Button(custom_id=OPEN_ID, label=label[:80], style=STYLES.get(style, STYLES["primary"]),
                                       emoji="🎫")
            button.callback = self._open
            self.add_item(button)

    async def _open(self, interaction: discord.Interaction) -> None:
        await self.cog.open_ticket(interaction, "Support")

    async def _select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []  # type: ignore[union-attr]
        await self.cog.open_ticket(interaction, values[0] if values else "Support")


class CloseView(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(custom_id=CLOSE_ID, label="Fermer le ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.close_from_interaction(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot
        bot.register_job("post_ticket_panel", self.job_post_panel)

    async def cog_load(self) -> None:
        # Une instance de chaque type suffit à router les clics de tous les panneaux existants
        self.bot.add_view(PanelView(self))
        self.bot.add_view(PanelView(self, categories=["Support"]))
        self.bot.add_view(CloseView(self))
        self.auto_close.start()

    async def cog_unload(self) -> None:
        self.auto_close.cancel()

    # ------------------------------------------------------------------ panneau
    async def build_panel(self, guild: discord.Guild) -> tuple[discord.Embed, PanelView]:
        conf = await self.bot.conf(guild.id, "tickets")
        values = utils.template_values(guild)
        embed = utils.build_embed(conf.panel_embed, values) if conf.panel_embed else None
        if embed is None:
            embed = discord.Embed(title=conf.panel_title, description=render_text(conf.panel_description, values),
                                  color=await self.bot.embed_color(guild.id))
        return embed, PanelView(self, conf.button_label, conf.button_style, conf.categories or None)

    async def job_post_panel(self, job: dict) -> tuple[bool, str]:
        guild: discord.Guild = job["guild"]
        channel = guild.get_channel(utils.snowflake(job["payload"].get("channel_id")) or 0)
        if not isinstance(channel, discord.TextChannel):
            return False, "Salon introuvable."
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return False, f"Il me manque « Envoyer des messages » ou « Intégrer des liens » dans #{channel.name}."
        if not guild.me.guild_permissions.manage_channels:
            return False, "Il me manque « Gérer les salons » pour créer les tickets."
        embed, view = await self.build_panel(guild)
        await channel.send(embed=embed, view=view)
        return True, f"Panneau posté dans #{channel.name}."

    # ------------------------------------------------------------------ ouverture
    async def open_ticket(self, interaction: discord.Interaction, category_name: str) -> None:
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            return
        if not self.bot.globally_enabled("tickets"):
            await utils.reply(interaction, utils.err("Les tickets sont temporairement indisponibles."))
            return
        conf = await self.bot.conf(guild.id, "tickets")
        if not conf.enabled:
            await utils.reply(interaction, utils.err("Les tickets sont désactivés sur ce serveur."))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        def count_open():
            with session_scope() as db:
                return db.scalar(select(func.count()).select_from(Ticket).where(
                    Ticket.guild_id == guild.id, Ticket.opener_id == user.id, Ticket.status == "open")) or 0
        if await self.bot.db(count_open) >= conf.max_open_per_user:
            await utils.reply(interaction, utils.err(f"Vous avez déjà {conf.max_open_per_user} ticket(s) ouvert(s)."))
            return
        category = guild.get_channel(utils.snowflake(conf.category_id) or 0)
        staff = [r for r in (guild.get_role(int(i)) for i in conf.staff_role_ids) if r]
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                  manage_channels=True, manage_messages=True, embed_links=True,
                                                  attach_files=True),
        }
        for role in staff:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True, attach_files=True,
                                                           manage_messages=True)

        def create_row():
            with session_scope() as db:
                t = Ticket(guild_id=guild.id, opener_id=user.id, category=category_name[:64])
                db.add(t)
                db.flush()
                return t.id
        ticket_id = await self.bot.db(create_row)
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{ticket_id}", overwrites=overwrites,
                category=category if isinstance(category, discord.CategoryChannel) else None,
                topic=f"Ticket n°{ticket_id} · {category_name} · ouvert par {user} ({user.id})",
                reason=f"Nexbot : ticket ouvert par {user}")
        except discord.HTTPException:
            def drop():
                with session_scope() as db:
                    row = db.get(Ticket, ticket_id)
                    if row:
                        db.delete(row)
            await self.bot.db(drop)
            await utils.reply(interaction, utils.err("Impossible de créer le salon : il me manque « Gérer les salons » "
                                                     "ou la catégorie est pleine (50 salons max)."))
            return

        def set_channel():
            with session_scope() as db:
                row = db.get(Ticket, ticket_id)
                if row:
                    row.channel_id = channel.id
        await self.bot.db(set_channel)
        values = utils.template_values(guild, user, {"ticket.id": ticket_id, "ticket.category": category_name})
        embed = discord.Embed(title=f"🎫 Ticket n°{ticket_id} — {category_name}",
                              description=render_text(conf.open_text, values)[:4000],
                              color=await self.bot.embed_color(guild.id))
        mentions = " ".join([user.mention] + [r.mention for r in staff])
        await channel.send(content=mentions, embed=embed, view=CloseView(self),
                           allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await utils.reply(interaction, utils.ok(f"Votre ticket est ouvert : {channel.mention}"))
        self.bot.record_usage(user.id, "ticket:open")
        self.bot.stat(guild.id, "tickets")
        await self._log(guild, conf, discord.Embed(title=f"🎫 Ticket n°{ticket_id} ouvert",
                                                   description=f"{user.mention} · {category_name} · {channel.mention}",
                                                   color=utils.SUCCESS))

    async def _log(self, guild: discord.Guild, conf, embed: discord.Embed, file: discord.File | None = None) -> None:
        channel = guild.get_channel(utils.snowflake(conf.log_channel_id) or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed, file=file)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ fermeture
    async def _ticket_for(self, channel_id: int) -> tuple[int, int, str] | None:
        def load():
            with session_scope() as db:
                t = db.scalars(select(Ticket).where(Ticket.channel_id == channel_id, Ticket.status == "open")).first()
                return (t.id, t.opener_id, t.category) if t else None
        return await self.bot.db(load)

    async def _can_close(self, member: discord.Member, opener_id: int) -> bool:
        if member.id == opener_id or member.guild_permissions.manage_channels:
            return True
        conf = await self.bot.conf(member.guild.id, "tickets")
        return any(str(r.id) in conf.staff_role_ids for r in member.roles)

    async def close_from_interaction(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return
        info = await self._ticket_for(channel.id)
        if info is None:
            await utils.reply(interaction, utils.err("Ce salon n'est pas un ticket ouvert."))
            return
        if not await self._can_close(interaction.user, info[1]):
            await utils.reply(interaction, utils.err("Seuls l'auteur du ticket et le staff peuvent le fermer."))
            return
        await interaction.response.send_message(embed=utils.warn_embed("🔒 Fermeture du ticket dans 5 secondes…"))
        await self.close_ticket(channel, info, interaction.user)

    async def close_ticket(self, channel: discord.TextChannel, info: tuple[int, int, str],
                           closer: discord.abc.User, reason: str = "") -> None:
        ticket_id, opener_id, category = info
        guild = channel.guild
        conf = await self.bot.conf(guild.id, "tickets")
        lines = []
        try:
            async for m in channel.history(limit=1000, oldest_first=True):
                stamp = m.created_at.strftime("%d/%m/%Y %H:%M")
                text = m.content or (" ".join(e.title or e.description or "" for e in m.embeds) if m.embeds else "")
                files = " ".join(a.url for a in m.attachments)
                lines.append(f"[{stamp}] {m.author} : {text} {files}".rstrip())
        except discord.HTTPException:
            pass
        transcript = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"ticket-{ticket_id}.txt")

        def close_row():
            with session_scope() as db:
                row = db.get(Ticket, ticket_id)
                if row:
                    row.status, row.closed_at, row.closed_by = "closed", utcnow(), closer.id
        await self.bot.db(close_row)
        embed = discord.Embed(title=f"🔒 Ticket n°{ticket_id} fermé", color=utils.DANGER,
                              description=f"Auteur : <@{opener_id}> · {category}\nFermé par : {closer.mention}"
                                          + (f"\n{reason}" if reason else "") + f"\nMessages : {len(lines)}")
        await self._log(guild, conf, embed, transcript)
        opener = guild.get_member(opener_id)
        if opener and conf.close_text:
            values = utils.template_values(guild, opener, {"ticket.id": ticket_id, "ticket.category": category})
            try:
                await opener.send(render_text(conf.close_text, values)[:2000])
            except discord.HTTPException:
                pass
        await discord.utils.sleep_until(utcnow() + timedelta(seconds=5))
        try:
            await channel.delete(reason=f"Nexbot : ticket n°{ticket_id} fermé par {closer}")
        except discord.HTTPException:
            pass

    @tasks.loop(minutes=30)
    async def auto_close(self) -> None:
        def load():
            with session_scope() as db:
                return [(t.id, t.guild_id, t.channel_id, t.opener_id, t.category)
                        for t in db.scalars(select(Ticket).where(Ticket.status == "open", Ticket.channel_id.is_not(None)))]
        for tid, gid, cid, opener, cat in await self.bot.db(load):
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            conf = await self.bot.conf(gid, "tickets")
            if not conf.auto_close_hours:
                continue
            channel = guild.get_channel(cid)
            if channel is None:  # salon supprimé à la main : on clôt l'entrée
                def close_row():
                    with session_scope() as db:
                        row = db.get(Ticket, tid)
                        if row:
                            row.status, row.closed_at = "closed", utcnow()
                await self.bot.db(close_row)
                continue
            if not isinstance(channel, discord.TextChannel):
                continue
            last = discord.utils.snowflake_time(channel.last_message_id) if channel.last_message_id else channel.created_at
            if utcnow() - last > timedelta(hours=conf.auto_close_hours):
                await self.close_ticket(channel, (tid, opener, cat), guild.me,
                                        f"Fermeture automatique après {conf.auto_close_hours} h d'inactivité")

    @auto_close.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ commandes
    ticket = app_commands.Group(name="ticket", description="Gestion des tickets", guild_only=True)

    @ticket.command(name="close", description="Fermer le ticket actuel")
    @app_commands.rename(reason="raison")
    async def t_close(self, interaction: discord.Interaction, reason: str = "") -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await utils.reply(interaction, utils.err("À utiliser dans un ticket."))
            return
        info = await self._ticket_for(channel.id)
        if info is None or not await self._can_close(interaction.user, info[1]):  # type: ignore[arg-type]
            await utils.reply(interaction, utils.err("Ce salon n'est pas un ticket que vous pouvez fermer."))
            return
        await interaction.response.send_message(embed=utils.warn_embed("🔒 Fermeture du ticket dans 5 secondes…"))
        await self.close_ticket(channel, info, interaction.user, reason)

    async def _staff_in_ticket(self, interaction: discord.Interaction) -> tuple[int, int, str] | None:
        channel = interaction.channel
        info = await self._ticket_for(channel.id) if isinstance(channel, discord.TextChannel) else None
        if info is None:
            await utils.reply(interaction, utils.err("À utiliser dans un ticket ouvert."))
            return None
        conf = await self.bot.conf(interaction.guild_id, "tickets")  # type: ignore[arg-type]
        member = interaction.user
        assert isinstance(member, discord.Member)
        if not (member.guild_permissions.manage_channels or any(str(r.id) in conf.staff_role_ids for r in member.roles)):
            await utils.reply(interaction, utils.err("Réservé au staff des tickets."))
            return None
        return info

    @ticket.command(name="add", description="Ajouter un membre au ticket")
    @app_commands.rename(member="membre")
    async def t_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if await self._staff_in_ticket(interaction) is None:
            return
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True,  # type: ignore[union-attr]
                                                  read_message_history=True, attach_files=True)
        await utils.reply(interaction, utils.ok(f"{member.mention} a été ajouté au ticket."), ephemeral=False)

    @ticket.command(name="remove", description="Retirer un membre du ticket")
    @app_commands.rename(member="membre")
    async def t_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        info = await self._staff_in_ticket(interaction)
        if info is None:
            return
        if member.id == info[1]:
            await utils.reply(interaction, utils.err("Impossible de retirer l'auteur du ticket : fermez-le plutôt."))
            return
        await interaction.channel.set_permissions(member, overwrite=None)  # type: ignore[union-attr]
        await utils.reply(interaction, utils.ok(f"{member.mention} a été retiré du ticket."), ephemeral=False)

    @ticket.command(name="panel", description="Poster le panneau des tickets dans ce salon (Gérer le serveur)")
    async def t_panel(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_moderator(interaction.user, "manage_guild"):  # type: ignore[arg-type]
            await utils.reply(interaction, utils.err("Permission « Gérer le serveur » requise."))
            return
        conf = await self.bot.conf(interaction.guild_id, "tickets")  # type: ignore[arg-type]
        if not conf.enabled:
            await utils.reply(interaction, utils.err("Activez d'abord les tickets dans le Dashboard."))
            return
        embed, view = await self.build_panel(interaction.guild)  # type: ignore[arg-type]
        await interaction.channel.send(embed=embed, view=view)  # type: ignore[union-attr]
        await utils.reply(interaction, utils.ok("Panneau posté."))


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Tickets(bot))
