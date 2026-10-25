"""Anti-raid et anti-nuke.

Ce que Nexbot peut faire : détecter des arrivées massives, des comptes trop récents,
des bots ajoutés sans autorisation, des suppressions / créations en rafale (via le
journal d'audit en temps réel), des webhooks suspects et des permissions dangereuses ;
puis isoler, expulser, retirer les rôles ou bannir l'auteur, et recréer la structure
supprimée (Premium).

Ce qu'aucun bot ne peut faire : empêcher une action avant que Discord ne l'exécute,
agir sur le propriétaire du serveur ou sur un membre placé au-dessus de son rôle,
ni restaurer des messages supprimés.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from ...core.db import session_scope
from ...core.models import SecurityIncident, utcnow
from .. import utils
from ..automod_logic import SlidingCounter
from ..client import Nexbot
from .logs import DANGEROUS, perm_diff

log = logging.getLogger("nexbot.antiraid")
A = discord.AuditLogAction

NUKE_RULES = {  # action d'audit -> (type d'incident, champ du seuil)
    A.channel_delete: ("channel_delete", "channel_delete_threshold"),
    A.channel_create: ("channel_create", "channel_create_threshold"),
    A.role_delete: ("role_delete", "role_delete_threshold"),
    A.role_create: ("role_create", "role_create_threshold"),
    A.ban: ("mass_ban", "ban_threshold"),
    A.kick: ("mass_ban", "ban_threshold"),
    A.webhook_create: ("webhook_create", "webhook_threshold"),
}
KIND_LABELS = {
    "join_raid": "Arrivée massive", "young_account": "Compte trop récent", "bot_added": "Bot ajouté sans autorisation",
    "channel_delete": "Suppressions de salons en rafale", "channel_create": "Créations de salons en rafale",
    "role_delete": "Suppressions de rôles en rafale", "role_create": "Créations de rôles en rafale",
    "mass_ban": "Bannissements / expulsions en rafale", "webhook_create": "Créations de webhooks en rafale",
    "dangerous_permission": "Permission dangereuse accordée", "mention_raid": "Spam de mentions coordonné",
    "quarantine": "Mise en quarantaine",
}
REQUIRED_PERMS = {"view_audit_log": "Voir les logs du serveur", "ban_members": "Bannir des membres",
                  "kick_members": "Expulser des membres", "manage_roles": "Gérer les rôles",
                  "manage_channels": "Gérer les salons", "manage_guild": "Gérer le serveur",
                  "moderate_members": "Exclure temporairement", "manage_messages": "Gérer les messages",
                  "manage_webhooks": "Gérer les webhooks"}
SEVERITY_COLORS = {"low": utils.INFO, "medium": utils.WARNING, "high": utils.DANGER, "critical": 0x8B0000}


@dataclass
class RaidState:
    until: float = 0.0
    previous_verification: discord.VerificationLevel | None = None
    reason: str = ""
    handled: set[int] = field(default_factory=set)


class AntiRaid(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot
        self.joins = SlidingCounter(300)
        self.actions = SlidingCounter(200)
        self.mentions = SlidingCounter(200)
        self.raids: dict[int, RaidState] = defaultdict(RaidState)
        self.deleted: dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        self.locked: dict[int, bool | None] = {}  # salon -> valeur précédente de send_messages pour @everyone

    async def cog_load(self) -> None:
        self.raid_watch.start()

    async def cog_unload(self) -> None:
        self.raid_watch.cancel()

    async def _conf(self, guild: discord.Guild):
        conf = await self.bot.conf(guild.id, "antiraid")
        return conf if conf.enabled and self.bot.globally_enabled("antiraid") else None

    def _trusted(self, guild: discord.Guild, conf, user_id: int | None) -> bool:
        if user_id is None:
            return True
        if user_id == guild.owner_id or (self.bot.user and user_id == self.bot.user.id):
            return True
        return str(user_id) in conf.trusted_user_ids

    # ================================================================== incidents & alertes
    async def incident(self, guild: discord.Guild, kind: str, severity: str, actor_id: int | None,
                       details: dict[str, Any], action_taken: str, conf=None) -> int:
        def save():
            with session_scope() as db:
                inc = SecurityIncident(guild_id=guild.id, kind=kind, severity=severity, actor_id=actor_id,
                                       details=details, action_taken=action_taken)
                db.add(inc)
                db.flush()
                return inc.id
        inc_id = await self.bot.db(save)
        self.bot.stat(guild.id, "incidents")
        if kind == "quarantine":
            return inc_id
        conf = conf or await self.bot.conf(guild.id, "antiraid")
        embed = discord.Embed(title=f"🚨 {KIND_LABELS.get(kind, kind)}", color=SEVERITY_COLORS.get(severity, utils.DANGER))
        if actor_id:
            embed.add_field(name="Auteur", value=f"<@{actor_id}> (`{actor_id}`)")
        embed.add_field(name="Gravité", value=severity.upper())
        embed.add_field(name="Réaction", value=action_taken)
        detail_lines = [f"**{k}** : {utils.truncate(str(v), 200)}" for k, v in details.items() if k != "roles"]
        if detail_lines:
            embed.add_field(name="Détails", value="\n".join(detail_lines)[:1000], inline=False)
        embed.set_footer(text=f"Incident n°{inc_id} · Dashboard → Incidents pour le marquer résolu")
        embed.timestamp = utcnow()
        channel = guild.get_channel(utils.snowflake(conf.alert_channel_id) or 0)
        if isinstance(channel, discord.TextChannel):
            role_id = utils.snowflake(conf.alert_role_id)
            try:
                await channel.send(content=f"<@&{role_id}>" if role_id else None, embed=embed,
                                   allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException:
                pass
        if conf.alert_dm_owner and severity in {"high", "critical"} and guild.owner:
            try:
                dm = embed.copy()
                dm.title = f"🚨 {guild.name} — {KIND_LABELS.get(kind, kind)}"
                await guild.owner.send(embed=dm)
            except discord.HTTPException:
                pass
        await self.bot.send_log(guild, "antiraid", embed.copy())
        if severity in {"high", "critical"}:
            await self.bot.send_log(guild, "security", embed.copy())
        automations = self.bot.get_cog("Automations")
        if automations is not None:
            await automations.fire(guild, "antiraid_alert", extra={"incident": KIND_LABELS.get(kind, kind)})  # type: ignore[attr-defined]
        return inc_id

    # ================================================================== quarantaine
    async def quarantine(self, member: discord.Member, reason: str, conf=None) -> str:
        """Isole un membre. Renvoie la réaction réellement appliquée."""
        guild = member.guild
        conf = conf or await self.bot.conf(guild.id, "antiraid")
        if member.id == guild.owner_id or member.top_role >= guild.me.top_role:
            return "impossible (rôle trop haut)"
        q_role = guild.get_role(utils.snowflake(conf.quarantine_role_id) or 0)
        removed = utils.manageable_roles(member)
        if q_role is not None and q_role < guild.me.top_role:
            try:
                keep = [r for r in member.roles if r not in removed and not r.is_default()]
                await member.edit(roles=list({*keep, q_role}), reason=f"Nexbot quarantaine : {reason}")
            except discord.HTTPException as exc:
                log.info("Quarantaine impossible (%s) : %s", guild.id, exc)
                return "échec (permissions)"
            await self.incident(guild, "quarantine", "low", member.id,
                                {"raison": reason, "roles": [r.id for r in removed]}, "quarantaine")
            return "quarantaine"
        # Pas de rôle de quarantaine configuré : exclusion temporaire de 24 h (le maximum utile)
        try:
            if not member.guild_permissions.administrator:
                await member.timeout(timedelta(hours=24), reason=f"Nexbot quarantaine : {reason}")
                return "exclusion 24 h (aucun rôle de quarantaine configuré)"
        except discord.HTTPException:
            pass
        return "échec (aucun rôle de quarantaine et exclusion impossible)"

    async def unquarantine(self, member: discord.Member, moderator_id: int) -> tuple[bool, str]:
        guild = member.guild
        conf = await self.bot.conf(guild.id, "antiraid")

        def load():
            with session_scope() as db:
                inc = db.scalars(select(SecurityIncident).where(
                    SecurityIncident.guild_id == guild.id, SecurityIncident.actor_id == member.id,
                    SecurityIncident.kind == "quarantine", SecurityIncident.resolved.is_(False))
                    .order_by(SecurityIncident.id.desc())).first()
                if inc is None:
                    return None
                inc.resolved, inc.resolved_by = True, moderator_id
                return list(inc.details.get("roles", []))
        role_ids = await self.bot.db(load)
        q_role = guild.get_role(utils.snowflake(conf.quarantine_role_id) or 0)
        restored = [r for r in (guild.get_role(rid) for rid in (role_ids or [])) if r and r < guild.me.top_role]
        try:
            if q_role and q_role in member.roles:
                await member.remove_roles(q_role, reason="Nexbot : fin de quarantaine")
            if restored:
                await member.add_roles(*restored, reason="Nexbot : fin de quarantaine (rôles restaurés)")
            if member.is_timed_out():
                await member.timeout(None, reason="Nexbot : fin de quarantaine")
        except discord.HTTPException as exc:
            return False, f"Discord a refusé : {exc}"
        if role_ids is None and not (q_role and q_role in member.roles):
            return True, "Membre libéré (aucune quarantaine enregistrée)."
        return True, f"Membre libéré, {len(restored)} rôle(s) restauré(s)."

    # ================================================================== mode protection renforcée
    def raid_active(self, guild_id: int) -> bool:
        return self.raids[guild_id].until > time.monotonic()

    async def start_raid(self, guild: discord.Guild, conf, reason: str, minutes: int | None = None) -> None:
        state = self.raids[guild.id]
        already = state.until > time.monotonic()
        state.until = time.monotonic() + 60 * (minutes or conf.raid_mode_minutes)
        state.reason = reason
        if already:
            return
        state.handled = set()
        if conf.join_raid_action == "lockdown" and guild.me.guild_permissions.manage_guild:
            try:
                state.previous_verification = guild.verification_level
                await guild.edit(verification_level=discord.VerificationLevel.highest,
                                 reason="Nexbot : mode protection renforcée")
            except discord.HTTPException as exc:
                log.info("Verrouillage impossible sur %s : %s", guild.id, exc)
                state.previous_verification = None

    async def end_raid(self, guild: discord.Guild, by: str = "fin du délai") -> None:
        state = self.raids.pop(guild.id, RaidState())
        if state.previous_verification is not None:
            try:
                await guild.edit(verification_level=state.previous_verification,
                                 reason="Nexbot : fin du mode protection renforcée")
            except discord.HTTPException:
                pass
        await self.bot.send_log(guild, "antiraid", discord.Embed(
            title="✅ Fin du mode protection renforcée", description=f"Raison : {by}", color=utils.SUCCESS))

    @tasks.loop(seconds=20)
    async def raid_watch(self) -> None:
        now = time.monotonic()
        for gid, state in list(self.raids.items()):
            if state.until and state.until <= now:
                guild = self.bot.get_guild(gid)
                if guild:
                    await self.end_raid(guild)
                else:
                    self.raids.pop(gid, None)
        self.joins.prune()
        self.actions.prune()
        self.mentions.prune()

    async def _apply_join_action(self, member: discord.Member, conf, action: str, reason: str) -> str:
        if action == "kick":
            if member.top_role >= member.guild.me.top_role:
                return "impossible (rôle trop haut)"
            try:
                await member.kick(reason=f"Nexbot anti-raid : {reason}")
                return "expulsion"
            except discord.HTTPException:
                return "échec de l'expulsion"
        if action in {"quarantine", "lockdown"}:
            return await self.quarantine(member, reason, conf)
        return "alerte"

    # ================================================================== arrivées
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        conf = await self._conf(guild)
        if conf is None:
            return
        if member.bot:
            await self._check_bot_added(member, conf)
            return

        if conf.join_protection:
            count = self.joins.hit(guild.id, conf.join_window_seconds, value=member.id)
            if count >= conf.join_threshold and not self.raid_active(guild.id):
                recent = [m for m in (guild.get_member(uid) for uid in self.joins.values(guild.id)) if m]
                await self.start_raid(guild, conf, f"{count} arrivées en {conf.join_window_seconds} s")
                state = self.raids[guild.id]
                results = []
                for m in recent:
                    state.handled.add(m.id)
                    results.append(await self._apply_join_action(m, conf, conf.join_raid_action, "arrivée massive"))
                await self.incident(guild, "join_raid", "high", None,
                                    {"arrivées": count, "fenêtre": f"{conf.join_window_seconds} s",
                                     "membres traités": len(recent), "durée du mode renforcé": f"{conf.raid_mode_minutes} min"},
                                    f"{conf.join_raid_action} ({', '.join(sorted(set(results))) or 'aucun membre'})", conf)
                return
            if self.raid_active(guild.id):
                state = self.raids[guild.id]
                if member.id not in state.handled:
                    state.handled.add(member.id)
                    await self._apply_join_action(member, conf, conf.join_raid_action, "arrivée pendant un raid")
                return

        if conf.min_account_age_days:
            age = utils.utcnow() - member.created_at
            if age < timedelta(days=conf.min_account_age_days):
                done = await self._apply_join_action(member, conf, conf.young_account_action, "compte trop récent")
                await self.incident(guild, "young_account", "low", member.id,
                                    {"âge du compte": utils.fmt_duration(age),
                                     "minimum": f"{conf.min_account_age_days} j"}, done, conf)

    async def _check_bot_added(self, bot_member: discord.Member, conf) -> None:
        guild = bot_member.guild
        if not conf.bot_add_protection or not guild.me.guild_permissions.view_audit_log:
            return
        adder = None
        await asyncio.sleep(1.5)  # laisser le temps au journal d'audit
        try:
            async for entry in guild.audit_logs(limit=10, action=A.bot_add):
                if entry.target and entry.target.id == bot_member.id:
                    adder = entry.user
                    break
        except discord.HTTPException:
            return
        if adder is None or self._trusted(guild, conf, adder.id):
            return
        action = "alerte"
        if bot_member.top_role < guild.me.top_role:
            try:
                await bot_member.kick(reason=f"Nexbot : bot ajouté par {adder} sans autorisation")
                action = "bot expulsé"
            except discord.HTTPException:
                action = "expulsion impossible"
        await self.incident(guild, "bot_added", "high", adder.id,
                            {"bot": f"{bot_member} ({bot_member.id})",
                             "règle": "seuls le propriétaire et les utilisateurs de confiance peuvent ajouter des bots"},
                            action, conf)

    # ================================================================== anti-nuke (journal d'audit)
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.deleted[channel.guild.id].append((time.monotonic(), "channel", self._snap_channel(channel)))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if not role.managed:
            self.deleted[role.guild.id].append((time.monotonic(), "role", {
                "name": role.name, "permissions": role.permissions, "color": role.color, "hoist": role.hoist,
                "mentionable": role.mentionable, "position": role.position}))

    @staticmethod
    def _snap_channel(ch: discord.abc.GuildChannel) -> dict:
        data: dict[str, Any] = {"id": ch.id, "name": ch.name, "type": ch.type, "position": ch.position,
                                "category_id": getattr(ch, "category_id", None), "overwrites": dict(ch.overwrites)}
        for attr in ("topic", "nsfw", "slowmode_delay", "bitrate", "user_limit"):
            if hasattr(ch, attr):
                data[attr] = getattr(ch, attr)
        return data

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        conf = await self._conf(guild)
        if conf is None or not conf.nuke_protection:
            return
        actor_id = entry.user_id
        if self._trusted(guild, conf, actor_id):
            return

        if entry.action == A.role_update and conf.permission_monitor:
            added, _ = perm_diff(getattr(entry.before, "permissions", None), getattr(entry.after, "permissions", None))
            dangerous = [p for p in added if p in DANGEROUS]
            if dangerous:
                role = entry.target
                await self.incident(guild, "dangerous_permission", "medium", actor_id,
                                    {"rôle": getattr(role, "name", getattr(role, "id", "?")),
                                     "permissions": ", ".join(dangerous)}, "alerte", conf)
            return

        rule = NUKE_RULES.get(entry.action)
        if rule is None:
            return
        kind, threshold_field = rule
        threshold = getattr(conf, threshold_field)
        count = self.actions.hit((guild.id, actor_id, kind), conf.action_window_seconds,
                                 value=getattr(entry.target, "id", None))
        if count < threshold:
            return
        targets = self.actions.values((guild.id, actor_id, kind))
        self.actions.reset((guild.id, actor_id, kind))
        await self._respond_nuke(guild, conf, actor_id, kind, count, targets)

    async def _respond_nuke(self, guild: discord.Guild, conf, actor_id: int | None, kind: str, count: int,
                            targets: list) -> None:
        member = guild.get_member(actor_id) if actor_id else None
        reaction = "alerte"
        strikes = 0
        if actor_id and conf.escalation_strikes:
            def count_strikes():
                with session_scope() as db:
                    return db.scalar(select(func.count()).select_from(SecurityIncident).where(
                        SecurityIncident.guild_id == guild.id, SecurityIncident.actor_id == actor_id,
                        SecurityIncident.kind.in_(list({k for k, _ in NUKE_RULES.values()})),
                        SecurityIncident.created_at >= utcnow() - timedelta(days=30))) or 0
            strikes = await self.bot.db(count_strikes) + 1
        want_ban = conf.nuke_action == "ban" or (conf.escalation_strikes and strikes >= conf.escalation_strikes)

        if want_ban and conf.confirm_destructive and actor_id:
            if member is None or member.top_role < guild.me.top_role:
                try:
                    await guild.ban(discord.Object(actor_id), reason=f"Nexbot anti-nuke : {KIND_LABELS[kind]}",
                                    delete_message_seconds=0)
                    reaction = "auteur banni" + (f" (récidive n°{strikes})" if strikes > 1 else "")
                except discord.HTTPException:
                    reaction = "bannissement impossible"
            else:
                reaction = "bannissement impossible (rôle trop haut)"
        elif conf.nuke_action in {"strip_roles", "ban"} and member is not None:
            if member.top_role >= guild.me.top_role:
                reaction = "impossible : rôle de l'auteur au-dessus de Nexbot"
            elif member.bot:
                try:
                    await member.kick(reason=f"Nexbot anti-nuke : {KIND_LABELS[kind]}")
                    reaction = "bot expulsé"
                except discord.HTTPException:
                    reaction = "expulsion du bot impossible"
            else:
                roles = utils.manageable_roles(member)
                try:
                    await member.remove_roles(*roles, reason=f"Nexbot anti-nuke : {KIND_LABELS[kind]}")
                    if not member.guild_permissions.administrator:
                        await member.timeout(timedelta(hours=1), reason="Nexbot anti-nuke")
                    reaction = f"{len(roles)} rôle(s) retiré(s)"
                except discord.HTTPException:
                    reaction = "retrait des rôles impossible"

        if kind == "webhook_create":
            reaction += await self._delete_webhooks(guild, [t for t in targets if t])
        restored = ""
        if conf.auto_restore and kind in {"channel_delete", "role_delete"}:
            restored = await self._restore(guild, "channel" if kind == "channel_delete" else "role",
                                           conf.action_window_seconds + 10)
        details = {"actions": count, "fenêtre": f"{conf.action_window_seconds} s"}
        if restored:
            details["restauration"] = restored
        severity = "critical" if kind in {"channel_delete", "role_delete", "mass_ban"} else "high"
        await self.incident(guild, kind, severity, actor_id, details, reaction, conf)

    async def _delete_webhooks(self, guild: discord.Guild, ids: list[int]) -> str:
        if not ids or not guild.me.guild_permissions.manage_webhooks:
            return ""
        n = 0
        try:
            for wh in await guild.webhooks():
                if wh.id in ids:
                    await wh.delete(reason="Nexbot anti-nuke : webhooks en rafale")
                    n += 1
        except discord.HTTPException:
            pass
        return f", {n} webhook(s) supprimé(s)" if n else ""

    async def _restore(self, guild: discord.Guild, what: str, window: float) -> str:
        now = time.monotonic()
        snaps = [d for t, kind, d in list(self.deleted[guild.id]) if kind == what and now - t <= window]
        self.deleted[guild.id] = deque([x for x in self.deleted[guild.id] if not (x[1] == what and now - x[0] <= window)],
                                       maxlen=100)
        ok_count = 0
        if what == "role":
            for d in sorted(snaps, key=lambda d: d["position"]):
                try:
                    await guild.create_role(name=d["name"], permissions=d["permissions"], color=d["color"],
                                            hoist=d["hoist"], mentionable=d["mentionable"],
                                            reason="Nexbot : restauration après nuke")
                    ok_count += 1
                except discord.HTTPException:
                    pass
            return f"{ok_count}/{len(snaps)} rôle(s) recréé(s)"
        # salons : catégories d'abord, pour y ranger les autres
        new_categories: dict[int | None, discord.CategoryChannel] = {}
        for d in sorted(snaps, key=lambda d: (d["type"] != discord.ChannelType.category, d["position"])):
            overwrites = {k: v for k, v in d["overwrites"].items()
                          if guild.get_role(k.id) is not None or guild.get_member(k.id) is not None}
            category = new_categories.get(d["category_id"]) or guild.get_channel(d["category_id"] or 0)
            kwargs: dict[str, Any] = {"name": d["name"], "overwrites": overwrites, "position": d["position"],
                                      "reason": "Nexbot : restauration après nuke"}
            try:
                if d["type"] == discord.ChannelType.category:
                    created = await guild.create_category(**kwargs)
                    new_categories[d["id"]] = created  # les salons restaurés de cette catégorie y retournent
                elif d["type"] in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                    created = await guild.create_voice_channel(
                        category=category if isinstance(category, discord.CategoryChannel) else None,
                        bitrate=min(d.get("bitrate") or 64000, guild.bitrate_limit), user_limit=d.get("user_limit") or 0,
                        **kwargs)
                else:
                    created = await guild.create_text_channel(
                        category=category if isinstance(category, discord.CategoryChannel) else None,
                        topic=d.get("topic"), nsfw=d.get("nsfw", False), slowmode_delay=d.get("slowmode_delay") or 0,
                        **kwargs)
                ok_count += 1
            except discord.HTTPException:
                pass
        return f"{ok_count}/{len(snaps)} salon(s) recréé(s) (sans les messages)"

    async def track_mention_spam(self, member: discord.Member) -> None:
        """Appelé par le filtre quand un membre dépasse la limite de mentions (Premium : détection coordonnée)."""
        guild = member.guild
        conf = await self._conf(guild)
        if conf is None or not conf.mention_raid_threshold:
            return
        self.mentions.hit(guild.id, 30, value=member.id)
        distinct = set(self.mentions.values(guild.id))
        if len(distinct) >= conf.mention_raid_threshold and not self.raid_active(guild.id):
            self.mentions.reset(guild.id)
            await self.start_raid(guild, conf, "spam de mentions coordonné")
            results = [await self.quarantine(m, "spam de mentions coordonné", conf)
                       for m in (guild.get_member(uid) for uid in distinct) if m]
            await self.incident(guild, "mention_raid", "high", None,
                                {"membres": len(distinct), "fenêtre": "30 s"},
                                f"mode renforcé + {', '.join(sorted(set(results)))}", conf)

    # ================================================================== commandes
    async def _staff(self, interaction: discord.Interaction, permission: str) -> bool:
        if not await self.bot.is_moderator(interaction.user, permission):  # type: ignore[arg-type]
            await utils.reply(interaction, utils.err("Vous n'avez pas la permission d'utiliser cette commande."))
            return False
        return True

    @app_commands.command(name="raidmode", description="Activer ou couper le mode protection renforcée")
    @app_commands.describe(state="on / off", minutes="Durée (par défaut : réglage du Dashboard)")
    @app_commands.rename(state="etat")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def raidmode(self, interaction: discord.Interaction, state: Literal["on", "off"], minutes: app_commands.Range[int, 1, 1440] | None = None) -> None:
        if not await self._staff(interaction, "manage_guild"):
            return
        guild = interaction.guild
        assert guild is not None
        conf = await self.bot.conf(guild.id, "antiraid")
        if state == "on":
            await self.start_raid(guild, conf, f"activé par {interaction.user}", minutes)
            await utils.reply(interaction, utils.warn_embed(
                f"🛡️ Mode protection renforcée **activé** pour {minutes or conf.raid_mode_minutes} min : "
                f"les nouveaux arrivants subissent l'action « {conf.join_raid_action} »."), ephemeral=False)
            await self.bot.send_log(guild, "antiraid", discord.Embed(
                title="🛡️ Mode protection renforcée activé", description=f"Par {interaction.user.mention}"))
        else:
            await self.end_raid(guild, f"coupé par {interaction.user}")
            await utils.reply(interaction, utils.ok("Mode protection renforcée **désactivé**."), ephemeral=False)

    async def _set_lock(self, channel: discord.TextChannel, lock: bool, reason: str) -> bool:
        everyone = channel.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        if lock:
            if overwrite.send_messages is False:
                return False
            self.locked[channel.id] = overwrite.send_messages
            overwrite.send_messages = False
            overwrite.send_messages_in_threads = False
            overwrite.add_reactions = False
        else:
            overwrite.send_messages = self.locked.pop(channel.id, None)
            overwrite.send_messages_in_threads = None
            overwrite.add_reactions = None
        await channel.set_permissions(everyone, overwrite=overwrite, reason=reason)
        return True

    @app_commands.command(name="lockdown", description="Verrouiller un salon ou tout le serveur")
    @app_commands.describe(channel="Salon (par défaut : celui-ci)", server="Verrouiller tous les salons textuels",
                           reason="Raison")
    @app_commands.rename(channel="salon", server="tout_le_serveur", reason="raison")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def lockdown(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                       server: bool = False, reason: str = "") -> None:
        if not await self._staff(interaction, "manage_channels"):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer()
        text = f"Nexbot lockdown par {interaction.user} : {reason or 'aucune raison'}"[:500]
        targets = guild.text_channels if server else [channel or interaction.channel]
        done = 0
        for ch in targets:
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).manage_channels:
                try:
                    done += await self._set_lock(ch, True, text)
                except discord.HTTPException:
                    pass
        await utils.reply(interaction, utils.warn_embed(f"🔒 {done} salon(s) verrouillé(s)." + (f"\nRaison : {reason}" if reason else "")),
                          ephemeral=False)
        await self.bot.send_log(guild, "antiraid", discord.Embed(title="🔒 Verrouillage",
                                                                 description=f"{done} salon(s) par {interaction.user.mention}"))

    @app_commands.command(name="unlock", description="Déverrouiller un salon ou tout le serveur")
    @app_commands.rename(channel="salon", server="tout_le_serveur")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                     server: bool = False) -> None:
        if not await self._staff(interaction, "manage_channels"):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer()
        targets = guild.text_channels if server else [channel or interaction.channel]
        done = 0
        for ch in targets:
            if isinstance(ch, discord.TextChannel) and ch.overwrites_for(guild.default_role).send_messages is False:
                try:
                    done += await self._set_lock(ch, False, f"Nexbot unlock par {interaction.user}")
                except discord.HTTPException:
                    pass
        await utils.reply(interaction, utils.ok(f"🔓 {done} salon(s) déverrouillé(s)."), ephemeral=False)

    @app_commands.command(name="quarantine", description="Isoler un membre suspect (rôles retirés, conservés pour plus tard)")
    @app_commands.rename(member="membre", reason="raison")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def quarantine_cmd(self, interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
        if not await self._staff(interaction, "manage_roles"):
            return
        error = utils.hierarchy_error(interaction.user, member, interaction.guild.me, "isoler")  # type: ignore[arg-type,union-attr]
        if error:
            await utils.reply(interaction, utils.err(error))
            return
        await interaction.response.defer()
        result = await self.quarantine(member, reason or f"par {interaction.user}")
        await utils.reply(interaction, utils.warn_embed(f"🧪 {member.mention} : {result}."), ephemeral=False)
        await self.bot.send_log(interaction.guild, "antiraid", discord.Embed(  # type: ignore[arg-type]
            title="🧪 Quarantaine", description=f"{member.mention} par {interaction.user.mention} : {result}\n{reason}"))

    @app_commands.command(name="unquarantine", description="Libérer un membre de la quarantaine et lui rendre ses rôles")
    @app_commands.rename(member="membre")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def unquarantine_cmd(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._staff(interaction, "manage_roles"):
            return
        await interaction.response.defer()
        ok, text = await self.unquarantine(member, interaction.user.id)
        await utils.reply(interaction, (utils.ok if ok else utils.err)(f"{member.mention} : {text}"), ephemeral=False)

    @app_commands.command(name="security", description="État de la protection de ce serveur")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        conf = await self.bot.conf(guild.id, "antiraid")
        me = guild.me
        missing = [label for perm, label in REQUIRED_PERMS.items() if not getattr(me.guild_permissions, perm)]
        higher = [r for r in guild.roles if r > me.top_role and not r.managed]
        embed = discord.Embed(title="🛡️ Sécurité du serveur", color=await self.bot.embed_color(guild.id))
        on = lambda b: "🟢" if b else "⚪"  # noqa: E731
        embed.add_field(name="Protections", value=(
            f"{on(conf.enabled)} Anti-raid\n{on(conf.join_protection)} Arrivées massives "
            f"({conf.join_threshold} / {conf.join_window_seconds} s)\n"
            f"{on(conf.nuke_protection)} Anti-nuke (réaction : {conf.nuke_action})\n"
            f"{on(conf.bot_add_protection)} Bots non autorisés\n{on(conf.permission_monitor)} Permissions dangereuses\n"
            f"{on(conf.min_account_age_days)} Âge minimum des comptes ({conf.min_account_age_days} j)"))
        embed.add_field(name="État", value=(
            f"Mode renforcé : **{'ACTIF' if self.raid_active(guild.id) else 'non'}**\n"
            f"Salon d'alertes : {f'<#{conf.alert_channel_id}>' if conf.alert_channel_id else '⚠️ non configuré'}\n"
            f"Rôle de quarantaine : {f'<@&{conf.quarantine_role_id}>' if conf.quarantine_role_id else '⚠️ non configuré'}"))
        embed.add_field(name="Permissions de Nexbot", inline=False,
                        value="✅ Toutes présentes" if not missing else "⚠️ Manquantes : " + ", ".join(missing))
        if higher:
            embed.add_field(name="Rôles au-dessus de Nexbot", inline=False, value=(
                f"{len(higher)} rôle(s) au-dessus de Nexbot : il ne pourra pas agir sur leurs membres. "
                "Montez le rôle de Nexbot le plus haut possible."))
        embed.set_footer(text="Aucun bot ne peut empêcher 100 % des raids : Nexbot réagit en quelques secondes "
                              "aux événements que Discord lui transmet.")
        await utils.reply(interaction, embed)


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(AntiRaid(bot))
