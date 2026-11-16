"""Messages de bienvenue / départ et rôles automatiques."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ...core.embeds import render_text
from .. import utils
from ..client import Nexbot

log = logging.getLogger("nexbot.welcome")


class Welcome(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot

    def _quarantined(self, member: discord.Member) -> bool:
        antiraid = self.bot.get_cog("AntiRaid")
        return bool(antiraid and antiraid.raid_active(member.guild.id))  # type: ignore[attr-defined]

    @staticmethod
    def render(guild: discord.Guild, member: discord.abc.User, text: str, embed_model, mention: bool,
               embeds_allowed: bool = True) -> tuple[str, discord.Embed | None]:
        """Texte et embed tels que le membre les verra (utilisé pour l'envoi réel et pour /welcome test)."""
        values = utils.template_values(guild, member)
        content = render_text(text, values)[:2000] if text else ""
        embed = utils.build_embed(embed_model, values) if embed_model and embeds_allowed else None
        if mention and embed is not None and member.mention not in content:
            content = f"{member.mention} {content}".strip()
        return content, embed

    @staticmethod
    def diagnose(guild: discord.Guild, conf, kind: str) -> list[str]:
        """Problèmes qui empêcheraient l'envoi du message (liste vide = tout est prêt)."""
        problems: list[str] = []
        if kind == "dm":
            if not conf.dm_enabled:
                problems.append("Le message privé de bienvenue est désactivé (option Premium du Dashboard).")
            elif not conf.dm_text:
                problems.append("Le texte du message privé est vide.")
            if not conf.enabled:
                problems.append("Le message de bienvenue est désactivé : le MP n'est envoyé que s'il est actif.")
            return problems
        enabled, channel_id, label = (conf.enabled, conf.channel_id, "de bienvenue") if kind == "welcome" \
            else (conf.leave_enabled, conf.leave_channel_id, "de départ")
        if not enabled:
            problems.append(f"Le message {label} est désactivé dans le Dashboard.")
        if not channel_id:
            problems.append(f"Aucun salon {label} n'est choisi dans le Dashboard.")
            return problems
        channel = guild.get_channel(utils.snowflake(channel_id) or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            problems.append(f"Le salon {label} choisi n'existe plus ou n'est pas un salon textuel.")
            return problems
        perms = channel.permissions_for(guild.me)
        if not perms.view_channel or not perms.send_messages:
            problems.append(f"Nexbot ne peut pas écrire dans {channel.mention} (permissions « Voir le salon » "
                            "et « Envoyer des messages »).")
        embed_model = conf.embed if kind == "welcome" else conf.leave_embed
        if embed_model and not perms.embed_links:
            problems.append(f"Nexbot n'a pas la permission « Intégrer des liens » dans {channel.mention} : "
                            "l'embed ne sera pas affiché.")
        return problems

    async def _send(self, channel_id: str, guild: discord.Guild, member: discord.abc.User, text: str,
                    embed_model, mention: bool) -> None:
        channel = guild.get_channel(utils.snowflake(channel_id) or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        perms = channel.permissions_for(guild.me)
        if not perms.send_messages:
            return
        content, embed = self.render(guild, member, text, embed_model, mention, perms.embed_links)
        if not content and embed is None:
            return
        try:
            await channel.send(content=content or None, embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=mention, roles=False, everyone=False))
        except discord.HTTPException as exc:
            log.info("Message de bienvenue non envoyé sur %s : %s", guild.id, exc)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if not member.bot:
            self.bot.stat(guild.id, "joins")
        if self.bot.globally_enabled("welcome") and not member.bot and not self._quarantined(member):
            conf = await self.bot.conf(guild.id, "welcome")
            if conf.enabled:
                await self._send(conf.channel_id, guild, member, conf.text, conf.embed, conf.mention_member)
                if conf.dm_enabled and conf.dm_text:
                    try:
                        await member.send(render_text(conf.dm_text, utils.template_values(guild, member))[:2000])
                    except discord.HTTPException:
                        pass
        if self.bot.globally_enabled("autoroles"):
            await self._autoroles(member)

    async def _autoroles(self, member: discord.Member) -> None:
        guild = member.guild
        conf = await self.bot.conf(guild.id, "autoroles")
        if not conf.enabled:
            return
        ids = conf.bot_role_ids if member.bot else conf.role_ids
        if not ids:
            return
        if not member.bot and conf.delay_seconds:
            await asyncio.sleep(conf.delay_seconds)
            member = guild.get_member(member.id)  # type: ignore[assignment]
            if member is None:
                return
        if not member.bot and conf.skip_quarantined:
            antiraid_conf = await self.bot.conf(guild.id, "antiraid")
            q_role = utils.snowflake(antiraid_conf.quarantine_role_id)
            if self._quarantined(member) or any(r.id == q_role for r in member.roles) or member.is_timed_out():
                return
        if member.pending:  # dépistage d'adhésion Discord non terminé : on attendra on_member_update
            return
        roles = [r for r in (guild.get_role(int(i)) for i in ids) if r and r < guild.me.top_role and not r.managed]
        if roles:
            try:
                await member.add_roles(*roles, reason="Nexbot : rôles automatiques")
            except discord.HTTPException as exc:
                log.info("Rôles automatiques impossibles sur %s : %s", guild.id, exc)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending and self.bot.globally_enabled("autoroles"):
            await self._autoroles(after)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if member.bot:
            return
        self.bot.stat(guild.id, "leaves")
        if not self.bot.globally_enabled("welcome"):
            return
        if self._quarantined(member):
            # Pendant un raid, l'anti-raid peut expulser des dizaines de comptes : pas de rafale de messages de départ.
            return
        conf = await self.bot.conf(guild.id, "welcome")
        if conf.leave_enabled:
            await self._send(conf.leave_channel_id, guild, member, conf.leave_text, conf.leave_embed, False)

    # ------------------------------------------------------------------ /welcome test
    welcome_group = app_commands.Group(name="welcome", description="Messages de bienvenue et de départ",
                                       guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @welcome_group.command(name="test", description="Voir le message de bienvenue ou de départ (visible par vous seul)")
    @app_commands.rename(kind="message")
    @app_commands.describe(kind="Message à prévisualiser")
    @app_commands.choices(kind=[app_commands.Choice(name="Bienvenue", value="welcome"),
                                app_commands.Choice(name="Départ", value="leave"),
                                app_commands.Choice(name="Message privé de bienvenue (Premium)", value="dm")])
    async def welcome_test(self, interaction: discord.Interaction,
                           kind: str = "welcome") -> None:
        guild, member = interaction.guild, interaction.user
        assert guild is not None and isinstance(member, discord.Member)
        if not (member.guild_permissions.manage_guild or member.id == guild.owner_id):
            await utils.reply(interaction, utils.err("Réservé aux membres ayant la permission « Gérer le serveur »."))
            return
        conf = await self.bot.conf(guild.id, "welcome")
        problems = self.diagnose(guild, conf, kind)
        if not self.bot.globally_enabled("welcome"):
            problems.insert(0, "Ce module est momentanément suspendu par l'équipe de Nexbot.")
        if kind == "welcome":
            content, embed = self.render(guild, member, conf.text, conf.embed, conf.mention_member)
        elif kind == "leave":
            content, embed = self.render(guild, member, conf.leave_text, conf.leave_embed, False)
        else:
            content, embed = self.render(guild, member, conf.dm_text, None, False)
        status = utils.warn_embed("**À corriger avant que ce message soit envoyé :**\n" +
                                  "\n".join(f"• {p}" for p in problems)) if problems else \
            utils.ok("Tout est prêt : voici le message tel que les membres le verront (avec vous comme exemple).")
        embeds = [status] + ([embed] if embed is not None else [])
        preview = f"**Aperçu :**\n{content}" if content else "*(aucun texte)*"
        await interaction.response.send_message(content=preview[:2000], embeds=embeds, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Welcome(bot))
