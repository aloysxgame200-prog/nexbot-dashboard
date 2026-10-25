"""Logs par catégorie, configurés dans le Dashboard (salon + couleur par catégorie).

Les actions sur les salons, rôles, permissions et sanctions manuelles sont lues
dans le journal d'audit en temps réel (événement on_audit_log_entry_create),
ce qui permet d'indiquer QUI a fait l'action. Nexbot a besoin de la permission
« Voir les logs du serveur ».
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .. import utils
from ..client import Nexbot

log = logging.getLogger("nexbot.logs")

A = discord.AuditLogAction
DANGEROUS = ("administrator", "ban_members", "kick_members", "manage_guild", "manage_roles", "manage_channels",
             "manage_webhooks", "mention_everyone", "moderate_members")


def perm_diff(before: discord.Permissions | None, after: discord.Permissions | None) -> tuple[list[str], list[str]]:
    b = dict(before or discord.Permissions.none())
    a = dict(after or discord.Permissions.none())
    added = [k for k, v in a.items() if v and not b.get(k)]
    removed = [k for k, v in b.items() if v and not a.get(k)]
    return added, removed


def fmt_perms(names: list[str]) -> str:
    return ", ".join(("**" + n + "**" if n in DANGEROUS else n).replace("_", " ") for n in names) or "—"


class Logs(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot

    async def send(self, guild: discord.Guild, category: str, embed: discord.Embed,
                   file: discord.File | None = None) -> bool:
        if not self.bot.globally_enabled("logs"):
            return False
        conf = await self.bot.conf(guild.id, "logs")
        if not getattr(conf, f"{category}_enabled", False):
            return False
        channel = guild.get_channel(utils.snowflake(getattr(conf, f"{category}_channel_id", "")) or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return False
        embed.color = discord.Color(utils.hex_to_int(getattr(conf, f"{category}_color", "#7C5CFF")))
        if embed.timestamp is None:
            embed.timestamp = utils.utcnow()
        if not conf.show_avatars:
            embed.set_thumbnail(url=None)
            if embed.author:
                embed.set_author(name=embed.author.name or "")
        try:
            await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException as exc:
            log.warning("Log %s non envoyé sur %s : %s", category, guild.id, exc)
            return False

    @staticmethod
    def _user_embed(title: str, user: discord.abc.User, description: str | None = None) -> discord.Embed:
        embed = discord.Embed(title=title, description=description)
        embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)
        return embed

    # ------------------------------------------------------------------ membres
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        age = utils.utcnow() - member.created_at
        embed = self._user_embed("📥 Arrivée", member, f"{member.mention} a rejoint le serveur.")
        embed.add_field(name="Compte créé", value=f"{utils.ts(member.created_at, 'R')}"
                                                  + (" ⚠️ récent" if age.days < 7 else ""))
        embed.add_field(name="Membres", value=str(member.guild.member_count))
        await self.send(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        embed = self._user_embed("📤 Départ", member, f"**{member}** a quitté le serveur.")
        if member.joined_at:
            embed.add_field(name="Arrivé", value=utils.ts(member.joined_at, "R"))
        roles = [r.mention for r in member.roles if not r.is_default()]
        if roles:
            embed.add_field(name="Rôles", value=" ".join(roles)[:1000], inline=False)
        await self.send(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            embed = self._user_embed("✏️ Pseudo modifié", after)
            embed.add_field(name="Avant", value=before.nick or "—")
            embed.add_field(name="Après", value=after.nick or "—")
            await self.send(after.guild, "members", embed)

    # ------------------------------------------------------------------ messages
    async def _ignored(self, guild: discord.Guild, channel_id: int) -> bool:
        conf = await self.bot.conf(guild.id, "logs")
        return str(channel_id) in conf.ignored_channel_ids

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or await self._ignored(message.guild, message.channel.id):
            return
        embed = self._user_embed("🗑️ Message supprimé", message.author,
                                 utils.truncate(message.content, 3500) or "*(aucun texte)*")
        embed.add_field(name="Salon", value=message.channel.mention)  # type: ignore[union-attr]
        embed.add_field(name="Envoyé", value=utils.ts(message.created_at, "R"))
        if message.attachments:
            embed.add_field(name="Pièces jointes", value="\n".join(a.filename for a in message.attachments)[:1000],
                            inline=False)
        await self.send(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.cached_message is not None or not payload.guild_id:
            return  # déjà traité par on_message_delete
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or await self._ignored(guild, payload.channel_id):
            return
        embed = discord.Embed(title="🗑️ Message supprimé",
                              description="Contenu indisponible : le message était trop ancien pour être en mémoire.")
        embed.add_field(name="Salon", value=f"<#{payload.channel_id}>")
        embed.add_field(name="ID du message", value=str(payload.message_id))
        await self.send(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None or await self._ignored(guild, payload.channel_id):
            return
        embed = discord.Embed(title="🧹 Suppression groupée",
                              description=f"**{len(payload.message_ids)}** messages supprimés dans <#{payload.channel_id}>.")
        await self.send(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        if await self._ignored(after.guild, after.channel.id):
            return
        embed = self._user_embed("✏️ Message modifié", after.author, f"[Aller au message]({after.jump_url})")
        embed.add_field(name="Avant", value=utils.truncate(before.content, 1000) or "—", inline=False)
        embed.add_field(name="Après", value=utils.truncate(after.content, 1000) or "—", inline=False)
        embed.add_field(name="Salon", value=after.channel.mention)  # type: ignore[union-attr]
        await self.send(after.guild, "messages", embed)

    # ------------------------------------------------------------------ journal d'audit
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        actor = entry.user
        by = f"par {actor.mention}" if actor else "par un utilisateur inconnu"
        target = entry.target
        tname = getattr(target, "mention", None) or getattr(target, "name", None) or f"`{getattr(target, 'id', '?')}`"
        act = entry.action
        is_me = actor is not None and self.bot.user is not None and actor.id == self.bot.user.id
        reason = f"\nRaison : {entry.reason}" if entry.reason else ""

        if act == A.channel_create:
            await self.send(guild, "channels", discord.Embed(title="➕ Salon créé", description=f"{tname} {by}{reason}"))
        elif act == A.channel_delete:
            name = getattr(entry.before, "name", None) or tname
            await self.send(guild, "channels", discord.Embed(title="➖ Salon supprimé",
                                                             description=f"**#{name}** {by}{reason}"))
        elif act == A.channel_update and hasattr(entry.before, "name") and hasattr(entry.after, "name"):
            await self.send(guild, "channels", discord.Embed(
                title="✏️ Salon renommé", description=f"**#{entry.before.name}** → {tname} {by}"))
        elif act in (A.overwrite_create, A.overwrite_update, A.overwrite_delete):
            subject = entry.extra
            label = {A.overwrite_create: "ajoutées", A.overwrite_update: "modifiées", A.overwrite_delete: "retirées"}[act]
            embed = discord.Embed(title=f"🔐 Permissions de salon {label}",
                                  description=f"Salon : {tname}\nPour : {getattr(subject, 'mention', getattr(subject, 'name', subject))}\n{by}")
            allow = getattr(entry.after, "allow", None)
            if allow is not None:
                embed.add_field(name="Autorisées", value=fmt_perms([k for k, v in allow if v])[:1000])
            await self.send(guild, "permissions", embed)
        elif act == A.role_create:
            await self.send(guild, "permissions", discord.Embed(title="➕ Rôle créé", description=f"{tname} {by}"))
        elif act == A.role_delete:
            name = getattr(entry.before, "name", None) or tname
            await self.send(guild, "permissions", discord.Embed(title="➖ Rôle supprimé", description=f"**@{name}** {by}"))
        elif act == A.role_update:
            added, removed = perm_diff(getattr(entry.before, "permissions", None), getattr(entry.after, "permissions", None))
            if added or removed:
                embed = discord.Embed(title="🔐 Permissions d'un rôle modifiées", description=f"Rôle : {tname}\n{by}")
                embed.add_field(name="Ajoutées", value=fmt_perms(added)[:1000])
                embed.add_field(name="Retirées", value=fmt_perms(removed)[:1000])
                await self.send(guild, "permissions", embed)
        elif act == A.member_role_update:
            added = [r.mention for r in getattr(entry.after, "roles", []) or []]
            removed = [r.mention for r in getattr(entry.before, "roles", []) or []]
            embed = discord.Embed(title="🏷️ Rôles d'un membre modifiés", description=f"Membre : {tname}\n{by}")
            if added:
                embed.add_field(name="Ajoutés", value=" ".join(added)[:1000])
            if removed:
                embed.add_field(name="Retirés", value=" ".join(removed)[:1000])
            await self.send(guild, "roles", embed)
        elif act in (A.ban, A.unban, A.kick) and not is_me:
            title = {A.ban: "🔨 Bannissement (manuel)", A.unban: "♻️ Débannissement (manuel)",
                     A.kick: "👢 Expulsion (manuelle)"}[act]
            await self.send(guild, "moderation", discord.Embed(title=title, description=f"Membre : {tname}\n{by}{reason}"))
        elif act == A.member_update and not is_me and hasattr(entry.after, "timed_out_until"):
            until = entry.after.timed_out_until
            title = "🔇 Exclusion temporaire (manuelle)" if until else "🔊 Fin d'exclusion (manuelle)"
            desc = f"Membre : {tname}\n{by}" + (f"\nJusqu'au : {utils.ts(until)}" if until else "") + reason
            await self.send(guild, "moderation", discord.Embed(title=title, description=desc))


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Logs(bot))
