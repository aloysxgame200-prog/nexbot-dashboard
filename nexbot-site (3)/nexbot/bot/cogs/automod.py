"""Filtre automatique : invitations, liens, mots interdits, mentions, spam, répétitions, majuscules."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from .. import utils
from ..automod_logic import SpamDetector, check_content
from ..client import Nexbot

log = logging.getLogger("nexbot.automod")


class AutoMod(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot
        self.spam = SpamDetector()
        self._notice_cooldown: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self.prune.start()

    async def cog_unload(self) -> None:
        self.prune.cancel()

    @tasks.loop(minutes=10)
    async def prune(self) -> None:
        self.spam.rate.prune()
        now = time.monotonic()
        self._notice_cooldown = {k: v for k, v in self._notice_cooldown.items() if now - v < 60}
        if len(self.spam.recent) > 20000:
            self.spam.recent.clear()

    @staticmethod
    def _exempt(message: discord.Message, conf) -> bool:
        member = message.author
        if not isinstance(member, discord.Member):
            return True
        perms = member.guild_permissions
        if perms.administrator or perms.manage_messages or member.id == message.guild.owner_id:  # type: ignore[union-attr]
            return True
        if str(message.channel.id) in conf.exempt_channel_ids or (
                isinstance(message.channel, discord.Thread) and str(message.channel.parent_id) in conf.exempt_channel_ids):
            return True
        return any(str(r.id) in conf.exempt_role_ids for r in member.roles)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or message.webhook_id:
            return
        self.bot.stat(message.guild.id, "messages")
        if not self.bot.globally_enabled("automod"):
            return
        conf = await self.bot.conf(message.guild.id, "moderation")
        if not (conf.enabled and conf.automod_enabled) or self._exempt(message, conf):
            return
        mentions = len(message.raw_mentions) + len(message.raw_role_mentions) + (5 if message.mention_everyone else 0)
        verdict = check_content(message.content, conf, mentions) or \
            self.spam.check(message.guild.id, message.author.id, message.content, conf)
        if verdict is None:
            return
        await self._punish(message, conf, verdict.reason, verdict.rule)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Contournement classique : envoyer un message propre puis le modifier. On revérifie le nouveau texte.

        Seules les règles de contenu s'appliquent (invitations, liens, mots interdits, majuscules) :
        une modification ne renvoie pas de notification, donc les mentions et le spam ne sont pas recomptés.
        """
        if after.author.bot or after.guild is None or after.webhook_id or before.content == after.content:
            return
        if not self.bot.globally_enabled("automod"):
            return
        conf = await self.bot.conf(after.guild.id, "moderation")
        if not (conf.enabled and conf.automod_enabled and conf.filter_edits) or self._exempt(after, conf):
            return
        verdict = check_content(after.content, conf, 0)
        if verdict is None:
            return
        await self._punish(after, conf, f"{verdict.reason} (message modifié)", verdict.rule)

    async def _punish(self, message: discord.Message, conf, reason: str, rule: str) -> None:
        guild = message.guild
        assert guild is not None and isinstance(message.author, discord.Member)
        member = message.author
        self.bot.stat(guild.id, "automod")
        try:
            if rule in {"spam", "duplicate"} and isinstance(message.channel, (discord.TextChannel, discord.Thread)):
                # efface la rafale récente de ce membre, pas seulement le dernier message
                since = discord.utils.utcnow() - timedelta(seconds=max(conf.spam_window_seconds, 10) + 5)
                await message.channel.purge(limit=30, after=since, check=lambda m: m.author.id == member.id,
                                            reason=f"Nexbot : {reason}")
            else:
                await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            return  # pas la permission « Gérer les messages » : on n'insiste pas

        # Avertissement visible, limité à un par minute et par membre pour ne pas créer de spam
        key = (guild.id, member.id)
        now = time.monotonic()
        if now - self._notice_cooldown.get(key, 0) > 60:
            self._notice_cooldown[key] = now
            try:
                await message.channel.send(f"{member.mention}, ton message a été supprimé : **{reason}**.",
                                           delete_after=8, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass

        moderation = self.bot.get_cog("Moderation")
        action = conf.automod_action
        if rule in {"spam", "mentions"} and action == "delete":
            action = "timeout"  # le spam et les mentions de masse méritent au minimum une exclusion courte
        if moderation is not None and action in {"warn", "timeout"} and member.top_role < guild.me.top_role:
            try:
                if action == "warn":
                    await moderation.sanction(guild, member, guild.me, "warn", f"Filtre automatique : {reason}",  # type: ignore[attr-defined]
                                              automatic=True)
                elif not member.is_timed_out() and not member.guild_permissions.administrator:
                    await moderation.sanction(guild, member, guild.me, "timeout", f"Filtre automatique : {reason}",  # type: ignore[attr-defined]
                                              timedelta(minutes=10), automatic=True)
            except Exception as exc:  # noqa: BLE001
                log.info("Sanction automatique impossible sur %s : %s", guild.id, exc)

        embed = discord.Embed(title="🧹 Message filtré", description=utils.truncate(message.content, 1500) or "*(vide)*")
        embed.set_author(name=f"{member} ({member.id})", icon_url=member.display_avatar.url)
        embed.add_field(name="Règle", value=reason)
        embed.add_field(name="Salon", value=message.channel.mention)  # type: ignore[union-attr]
        await self.bot.send_log(guild, "moderation", embed)

        antiraid = self.bot.get_cog("AntiRaid")
        if antiraid is not None and rule == "mentions":
            await antiraid.track_mention_spam(member)  # type: ignore[attr-defined]


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(AutoMod(bot))
