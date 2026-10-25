"""Messages de bienvenue / départ et rôles automatiques."""
from __future__ import annotations

import asyncio
import logging

import discord
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

    async def _send(self, channel_id: str, guild: discord.Guild, member: discord.abc.User, text: str,
                    embed_model, mention: bool) -> None:
        channel = guild.get_channel(utils.snowflake(channel_id) or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        perms = channel.permissions_for(guild.me)
        if not perms.send_messages:
            return
        values = utils.template_values(guild, member)
        content = render_text(text, values)[:2000] if text else ""
        embed = utils.build_embed(embed_model, values) if embed_model and perms.embed_links else None
        if mention and embed is not None and member.mention not in content:
            content = f"{member.mention} {content}".strip()
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
        conf = await self.bot.conf(guild.id, "welcome")
        if conf.leave_enabled:
            await self._send(conf.leave_channel_id, guild, member, conf.leave_text, conf.leave_embed, False)


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Welcome(bot))
