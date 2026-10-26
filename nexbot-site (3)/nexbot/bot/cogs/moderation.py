"""Modération : sanctions, historique, notes, escalade automatique, bannissements temporaires."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from ...core import premium as premium_mod
from ...core.db import session_scope
from ...core.embeds import render_text
from ...core.models import ModNote, Sanction, utcnow
from .. import utils
from ..client import Nexbot

log = logging.getLogger("nexbot.moderation")

LABELS = {"warn": "Avertissement", "timeout": "Exclusion temporaire", "untimeout": "Fin d'exclusion",
          "kick": "Expulsion", "ban": "Bannissement", "tempban": "Bannissement temporaire", "unban": "Débannissement"}
ICONS = {"warn": "⚠️", "timeout": "🔇", "untimeout": "🔊", "kick": "👢", "ban": "🔨", "tempban": "⏳", "unban": "♻️"}
MAX_TIMEOUT = timedelta(days=28)


class ModerationError(Exception):
    """Erreur présentable à l'utilisateur."""


class Moderation(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.expire_sanctions.start()

    async def cog_unload(self) -> None:
        self.expire_sanctions.cancel()

    # ================================================================== service (utilisé aussi par automod, anti-raid…)
    async def _record(self, guild_id: int, user_id: int, moderator_id: int, kind: str, reason: str,
                      automatic: bool, expires_at=None) -> int:
        def save():
            with session_scope() as db:
                s = Sanction(guild_id=guild_id, user_id=user_id, moderator_id=moderator_id, type=kind,
                             reason=reason[:1000], automatic=automatic, expires_at=expires_at)
                db.add(s)
                db.flush()
                return s.id
        case_id = await self.bot.db(save)
        self.bot.stat(guild_id, "sanctions")
        return case_id

    async def active_warns(self, guild_id: int, user_id: int) -> int:
        conf = await self.bot.conf(guild_id, "moderation")

        def count():
            with session_scope() as db:
                q = select(func.count()).select_from(Sanction).where(
                    Sanction.guild_id == guild_id, Sanction.user_id == user_id, Sanction.type == "warn",
                    Sanction.active.is_(True))
                if conf.warn_expiry_days:
                    q = q.where(Sanction.created_at >= utcnow() - timedelta(days=conf.warn_expiry_days))
                return db.scalar(q) or 0
        return await self.bot.db(count)

    async def _notify(self, guild: discord.Guild, user: discord.abc.User, kind: str, reason: str,
                      duration: timedelta | None, moderator: discord.abc.User, case_id: int) -> bool:
        conf = await self.bot.conf(guild.id, "moderation")
        if not conf.dm_on_sanction or kind in {"unban", "untimeout"}:
            return False
        template = {"warn": conf.msg_warn, "timeout": conf.msg_timeout, "kick": conf.msg_kick,
                    "ban": conf.msg_ban, "tempban": conf.msg_ban}.get(kind)
        if not template:
            return False
        values = utils.template_values(guild, user, {"moderator": str(moderator), "reason": reason or "Aucune raison",
                                                     "duration": utils.fmt_duration(duration), "case_id": case_id})
        embed = discord.Embed(title=f"{ICONS.get(kind, '')} {LABELS[kind]} — {guild.name}",
                              description=render_text(template, values)[:4000],
                              color=await self.bot.embed_color(guild.id))
        embed.set_footer(text=f"Sanction n°{case_id}")
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False  # MP fermés

    async def _log(self, guild: discord.Guild, kind: str, target: discord.abc.User, moderator: discord.abc.User,
                   reason: str, duration: timedelta | None, case_id: int, automatic: bool) -> None:
        embed = discord.Embed(title=f"{ICONS.get(kind, '')} {LABELS[kind]} · n°{case_id}")
        embed.set_author(name=f"{target} ({target.id})", icon_url=target.display_avatar.url)
        embed.add_field(name="Membre", value=target.mention)
        embed.add_field(name="Par", value=f"{moderator.mention}{' (automatique)' if automatic else ''}")
        if kind in {"timeout", "tempban"}:
            embed.add_field(name="Durée", value=utils.fmt_duration(duration))
        embed.add_field(name="Raison", value=utils.truncate(reason, 1000) or "Aucune raison", inline=False)
        await self.bot.send_log(guild, "moderation", embed)

    async def sanction(self, guild: discord.Guild, target: discord.Member | discord.User, moderator: discord.abc.User,
                       kind: str, reason: str = "", duration: timedelta | None = None, automatic: bool = False,
                       delete_days: int = 0, escalate: bool = True) -> tuple[int, bool]:
        """Applique une sanction et l'enregistre. Renvoie (n° de sanction, MP envoyé)."""
        me = guild.me
        reason = (reason or "").strip()
        audit_reason = f"{moderator} : {reason or 'aucune raison'}"[:500]
        member = target if isinstance(target, discord.Member) else guild.get_member(target.id)
        if kind in {"kick", "timeout", "untimeout"} and member is None:
            raise ModerationError("Ce membre n'est pas sur le serveur.")
        if member is not None and kind not in {"unban", "untimeout"}:
            if member.id == guild.owner_id:
                raise ModerationError("Impossible de sanctionner le propriétaire du serveur.")
            if member.top_role >= me.top_role and kind != "warn":
                raise ModerationError("Le rôle de ce membre est au-dessus du mien : je ne peux pas le sanctionner.")
        expires_at = utcnow() + duration if duration and kind in {"timeout", "tempban"} else None

        dm_sent = False
        case_id = await self._record(guild.id, target.id, moderator.id, kind, reason, automatic, expires_at)
        if kind in {"kick", "ban", "tempban"}:  # prévenir AVANT : après, plus de serveur en commun
            dm_sent = await self._notify(guild, target, kind, reason, duration, moderator, case_id)
        try:
            if kind == "kick":
                await member.kick(reason=audit_reason)  # type: ignore[union-attr]
            elif kind in {"ban", "tempban"}:
                await guild.ban(target, reason=audit_reason, delete_message_seconds=min(delete_days, 7) * 86400)
            elif kind == "unban":
                await guild.unban(target, reason=audit_reason)
            elif kind == "timeout":
                await member.timeout(min(duration or timedelta(hours=1), MAX_TIMEOUT), reason=audit_reason)  # type: ignore[union-attr]
            elif kind == "untimeout":
                await member.timeout(None, reason=audit_reason)  # type: ignore[union-attr]
        except discord.Forbidden:
            await self._cancel(case_id)
            raise ModerationError("Discord m'a refusé cette action (permissions ou position de mon rôle).")
        except discord.NotFound:
            await self._cancel(case_id)
            raise ModerationError("Utilisateur introuvable" + (" ou non banni." if kind == "unban" else "."))
        if kind in {"warn", "timeout"}:
            dm_sent = await self._notify(guild, target, kind, reason, duration, moderator, case_id)
        await self._log(guild, kind, target, moderator, reason, duration, case_id, automatic)

        if kind == "warn" and member is not None:
            count = await self.active_warns(guild.id, member.id)
            automations = self.bot.get_cog("Automations")
            if automations:
                await automations.fire(guild, "warn_count", member=member, warn_count=count)  # type: ignore[attr-defined]
            if escalate:
                await self._escalate(guild, member, count)
        return case_id, dm_sent

    async def _cancel(self, case_id: int) -> None:
        def delete():
            with session_scope() as db:
                row = db.get(Sanction, case_id)
                if row:
                    db.delete(row)
        await self.bot.db(delete)

    async def _escalate(self, guild: discord.Guild, member: discord.Member, count: int) -> None:
        conf = await self.bot.conf(guild.id, "moderation")
        if not conf.escalation_enabled:
            return
        me = guild.me
        steps = [("ban", conf.warns_to_ban), ("kick", conf.warns_to_kick), ("timeout", conf.warns_to_timeout)]
        for kind, threshold in steps:
            if threshold and count == threshold:
                reason = f"Sanction automatique : {count} avertissements"
                try:
                    await self.sanction(guild, member, me, kind, reason,
                                        timedelta(minutes=conf.timeout_minutes) if kind == "timeout" else None,
                                        automatic=True, escalate=False)
                except ModerationError as exc:
                    log.info("Escalade impossible sur %s : %s", guild.id, exc)
                return

    @tasks.loop(seconds=60)
    async def expire_sanctions(self) -> None:
        """Lève les bannissements temporaires échus et clôt les timeouts terminés."""
        now = utcnow()

        def due():
            with session_scope() as db:
                rows = db.scalars(select(Sanction).where(Sanction.active.is_(True), Sanction.expires_at.is_not(None),
                                                         Sanction.expires_at <= now,
                                                         Sanction.type.in_(["tempban", "timeout"])).limit(100)).all()
                return [(r.id, r.guild_id, r.user_id, r.type) for r in rows]
        for case_id, gid, uid, kind in await self.bot.db(due):
            guild = self.bot.get_guild(gid)
            if guild is not None and kind == "tempban":
                try:
                    await guild.unban(discord.Object(uid), reason=f"Fin du bannissement temporaire n°{case_id}")
                    unban_id = await self._record(gid, uid, guild.me.id, "unban",
                                                  f"Fin du bannissement temporaire n°{case_id}", True)
                    user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                    await self._log(guild, "unban", user, guild.me, f"Fin du bannissement temporaire n°{case_id}",
                                    None, unban_id, True)
                except discord.NotFound:
                    pass  # déjà débanni manuellement
                except discord.HTTPException as exc:
                    log.warning("Débannissement automatique impossible (%s/%s) : %s", gid, uid, exc)
                    continue

            def close():
                with session_scope() as db:
                    row = db.get(Sanction, case_id)
                    if row:
                        row.active = False
            await self.bot.db(close)

    @expire_sanctions.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ================================================================== garde-fous des commandes
    async def _guard(self, interaction: discord.Interaction, permission: str, reason: str | None = "") -> bool:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not self.bot.globally_enabled("moderation"):
            await utils.reply(interaction, utils.err("La modération est temporairement indisponible."))
            return False
        conf = await self.bot.conf(interaction.guild.id, "moderation")
        if not conf.enabled:
            await utils.reply(interaction, utils.err("Le module de modération est désactivé (Dashboard → Modération)."))
            return False
        if not await self.bot.is_moderator(interaction.user, permission):
            await utils.reply(interaction, utils.err("Vous n'avez pas la permission d'utiliser cette commande."))
            return False
        if reason is not None and conf.require_reason and not (reason or "").strip():
            await utils.reply(interaction, utils.err("Une raison est obligatoire sur ce serveur."))
            return False
        return True

    async def _check_target(self, interaction: discord.Interaction, target: discord.Member | discord.User,
                            action: str) -> bool:
        error = utils.hierarchy_error(interaction.user, target, interaction.guild.me, action)  # type: ignore[arg-type,union-attr]
        if error:
            await utils.reply(interaction, utils.err(error))
            return False
        return True

    async def _done(self, interaction: discord.Interaction, kind: str, target: discord.abc.User, case_id: int,
                    dm_sent: bool, extra: str = "") -> None:
        text = f"{ICONS[kind]} **{LABELS[kind]}** appliqué(e) à {target.mention} — sanction n°{case_id}{extra}"
        if kind in {"warn", "timeout", "kick", "ban", "tempban"}:
            text += "\n" + ("📨 Le membre a été prévenu en MP." if dm_sent else "📭 MP non envoyé (fermés ou désactivés).")
        embed = discord.Embed(description=text, color=await self.bot.embed_color(interaction.guild_id))
        await utils.reply(interaction, embed, ephemeral=False)

    # ================================================================== commandes
    @app_commands.command(name="ban", description="Bannir un utilisateur (définitivement ou temporairement)")
    @app_commands.describe(user="Membre ou utilisateur à bannir", reason="Raison",
                           duration="Durée pour un ban temporaire (ex. 7j, 12h). Vide = définitif",
                           delete_days="Supprimer ses messages des X derniers jours (0-7)")
    @app_commands.rename(user="utilisateur", reason="raison", duration="durée", delete_days="supprimer_messages")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str = "",
                  duration: str | None = None, delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        if not await self._guard(interaction, "ban_members", reason):
            return
        target = interaction.guild.get_member(user.id) or user  # type: ignore[union-attr]
        if not await self._check_target(interaction, target, "bannir"):
            return
        delta = utils.parse_duration(duration) if duration else None
        if duration and delta is None:
            await utils.reply(interaction, utils.err("Durée invalide. Exemples : `30m`, `12h`, `7j`, `2w`."))
            return
        await interaction.response.defer()
        try:
            case_id, dm = await self.sanction(interaction.guild, target, interaction.user, "tempban" if delta else "ban",  # type: ignore[arg-type]
                                              reason, delta, delete_days=delete_days)
        except ModerationError as exc:
            await utils.reply(interaction, utils.err(str(exc)))
            return
        await self._done(interaction, "tempban" if delta else "ban", target, case_id, dm,
                         f" ({utils.fmt_duration(delta)})" if delta else "")

    @app_commands.command(name="unban", description="Débannir un utilisateur")
    @app_commands.describe(user_id="Identifiant de l'utilisateur banni", reason="Raison")
    @app_commands.rename(user_id="id_utilisateur", reason="raison")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "") -> None:
        if not await self._guard(interaction, "ban_members", None):
            return
        uid = utils.snowflake(user_id)
        if uid is None:
            await utils.reply(interaction, utils.err("Identifiant invalide."))
            return
        await interaction.response.defer()
        try:
            user = await self.bot.fetch_user(uid)
            case_id, dm = await self.sanction(interaction.guild, user, interaction.user, "unban", reason)  # type: ignore[arg-type]
        except discord.NotFound:
            await utils.reply(interaction, utils.err("Utilisateur introuvable."))
            return
        except ModerationError as exc:
            await utils.reply(interaction, utils.err(str(exc)))
            return

        def close_tempbans():
            with session_scope() as db:
                for s in db.scalars(select(Sanction).where(Sanction.guild_id == interaction.guild_id,
                                                           Sanction.user_id == uid, Sanction.type == "tempban",
                                                           Sanction.active.is_(True))):
                    s.active = False
        await self.bot.db(close_tempbans)
        await self._done(interaction, "unban", user, case_id, dm)

    @app_commands.command(name="kick", description="Expulser un membre")
    @app_commands.rename(member="membre", reason="raison")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
        if not await self._guard(interaction, "kick_members", reason) or \
                not await self._check_target(interaction, member, "expulser"):
            return
        await interaction.response.defer()
        try:
            case_id, dm = await self.sanction(interaction.guild, member, interaction.user, "kick", reason)  # type: ignore[arg-type]
        except ModerationError as exc:
            await utils.reply(interaction, utils.err(str(exc)))
            return
        await self._done(interaction, "kick", member, case_id, dm)

    @app_commands.command(name="timeout", description="Exclure temporairement un membre (28 jours maximum)")
    @app_commands.describe(duration="Ex. 10m, 2h, 1j (28 jours max)")
    @app_commands.rename(member="membre", duration="durée", reason="raison")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: str = "") -> None:
        if not await self._guard(interaction, "moderate_members", reason) or \
                not await self._check_target(interaction, member, "exclure"):
            return
        delta = utils.parse_duration(duration)
        if delta is None or delta > MAX_TIMEOUT:
            await utils.reply(interaction, utils.err("Durée invalide (entre 1 minute et 28 jours, ex. `10m`, `2h`, `1j`)."))
            return
        if member.guild_permissions.administrator:
            await utils.reply(interaction, utils.err("Discord ne permet pas d'exclure un administrateur."))
            return
        await interaction.response.defer()
        try:
            case_id, dm = await self.sanction(interaction.guild, member, interaction.user, "timeout", reason, delta)  # type: ignore[arg-type]
        except ModerationError as exc:
            await utils.reply(interaction, utils.err(str(exc)))
            return
        await self._done(interaction, "timeout", member, case_id, dm, f" ({utils.fmt_duration(delta)})")

    @app_commands.command(name="untimeout", description="Annuler l'exclusion temporaire d'un membre")
    @app_commands.rename(member="membre", reason="raison")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return
        if not member.is_timed_out():
            await utils.reply(interaction, utils.err("Ce membre n'est pas exclu temporairement."))
            return
        await interaction.response.defer()
        try:
            case_id, dm = await self.sanction(interaction.guild, member, interaction.user, "untimeout", reason)  # type: ignore[arg-type]
        except ModerationError as exc:
            await utils.reply(interaction, utils.err(str(exc)))
            return
        await self._done(interaction, "untimeout", member, case_id, dm)

    @app_commands.command(name="warn", description="Avertir un membre (sanctions automatiques selon le nombre)")
    @app_commands.rename(member="membre", reason="raison")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
        if not await self._guard(interaction, "moderate_members", reason) or \
                not await self._check_target(interaction, member, "avertir"):
            return
        if member.bot:
            await utils.reply(interaction, utils.err("On n'avertit pas un bot."))
            return
        await interaction.response.defer()
        try:
            case_id, dm = await self.sanction(interaction.guild, member, interaction.user, "warn", reason)  # type: ignore[arg-type]
        except ModerationError as exc:
            await utils.reply(interaction, utils.err(str(exc)))
            return
        count = await self.active_warns(interaction.guild_id, member.id)  # type: ignore[arg-type]
        await self._done(interaction, "warn", member, case_id, dm, f"\nAvertissements actifs : **{count}**")

    @app_commands.command(name="unwarn", description="Retirer un avertissement")
    @app_commands.describe(case_id="Numéro de la sanction (voir /warnings)")
    @app_commands.rename(case_id="numero")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def unwarn(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return
        status, uid = await self.revoke_warn(interaction.guild_id, case_id, interaction.user.id)  # type: ignore[arg-type]
        if status == "missing":
            await utils.reply(interaction, utils.err("Avertissement introuvable sur ce serveur (vérifiez le numéro avec /warnings)."))
            return
        if status == "already":
            await utils.reply(interaction, utils.err(f"L'avertissement n°{case_id} est déjà retiré."))
            return
        await utils.reply(interaction, utils.ok(f"Avertissement n°{case_id} retiré (<@{uid}>)."), ephemeral=False)
        await self.bot.send_log(interaction.guild, "moderation", discord.Embed(  # type: ignore[arg-type]
            title=f"↩️ Avertissement n°{case_id} retiré", description=f"Membre : <@{uid}>\nPar : {interaction.user.mention}"))

    async def revoke_warn(self, guild_id: int, case_id: int, by: int) -> tuple[str, int | None]:
        """Retire un avertissement. Statut : « ok », « missing » (introuvable ici) ou « already » (déjà retiré)."""
        def revoke():
            with session_scope() as db:
                s = db.get(Sanction, case_id)
                if s is None or s.guild_id != guild_id or s.type != "warn":
                    return "missing", None
                if not s.active:
                    return "already", s.user_id
                s.active, s.revoked_at, s.revoked_by = False, utcnow(), by
                return "ok", s.user_id
        return await self.bot.db(revoke)

    async def revoke_all_warns(self, guild_id: int, user_id: int, by: int) -> int:
        """Retire tous les avertissements actifs d'un membre. Renvoie le nombre retiré (l'historique est conservé)."""
        def revoke():
            with session_scope() as db:
                rows = db.scalars(select(Sanction).where(Sanction.guild_id == guild_id, Sanction.user_id == user_id,
                                                         Sanction.type == "warn", Sanction.active.is_(True))).all()
                now = utcnow()
                for s in rows:
                    s.active, s.revoked_at, s.revoked_by = False, now, by
                return len(rows)
        return await self.bot.db(revoke)

    @app_commands.command(name="clearwarns", description="Retirer tous les avertissements actifs d'un membre")
    @app_commands.describe(member="Membre ou utilisateur concerné", reason="Raison (affichée dans les logs)")
    @app_commands.rename(member="utilisateur", reason="raison")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def clearwarns(self, interaction: discord.Interaction, member: discord.User, reason: str = "") -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return
        count = await self.revoke_all_warns(interaction.guild_id, member.id, interaction.user.id)  # type: ignore[arg-type]
        if not count:
            await utils.reply(interaction, utils.err(f"{member.mention} n'a aucun avertissement actif."))
            return
        await utils.reply(interaction, utils.ok(f"{count} avertissement(s) retiré(s) pour {member.mention}. "
                                                "Ils restent visibles, barrés, dans /history."), ephemeral=False)
        await self.bot.send_log(interaction.guild, "moderation", discord.Embed(  # type: ignore[arg-type]
            title=f"↩️ {count} avertissement(s) retiré(s)",
            description=f"Membre : {member.mention}\nPar : {interaction.user.mention}"
                        + (f"\nRaison : {utils.truncate(reason, 500)}" if reason else "")))

    async def _history_embed(self, guild_id: int, user: discord.abc.User, only_warns: bool) -> discord.Embed:
        premium = await self.bot.is_premium(guild_id)
        days = premium_mod.limits(premium)["history_days"]

        def load():
            with session_scope() as db:
                q = select(Sanction).where(Sanction.guild_id == guild_id, Sanction.user_id == user.id,
                                           Sanction.created_at >= utcnow() - timedelta(days=days))
                if only_warns:
                    q = q.where(Sanction.type == "warn", Sanction.active.is_(True))
                return [(s.id, s.type, s.reason, s.created_at, s.moderator_id, s.automatic, s.active)
                        for s in db.scalars(q.order_by(Sanction.id.desc()).limit(25))]
        rows = await self.bot.db(load)
        embed = discord.Embed(title=("Avertissements actifs" if only_warns else "Historique") + f" — {user}",
                              color=await self.bot.embed_color(guild_id))
        embed.set_thumbnail(url=user.display_avatar.url)
        lines = [f"`n°{cid}` {ICONS.get(kind, '')} **{LABELS.get(kind, kind)}** {utils.ts(at, 'd')} par <@{mod}>"
                 f"{' (auto)' if auto else ''}{'' if active else ' ~~retiré~~'}\n↳ {utils.truncate(reason, 120) or 'Aucune raison'}"
                 for cid, kind, reason, at, mod, auto, active in rows]
        embed.description = "\n".join(lines)[:4000] or "Aucune sanction."
        embed.set_footer(text="Historique illimité (Premium)" if premium else f"Historique des {days} derniers jours")
        return embed

    @app_commands.command(name="warnings", description="Avertissements actifs d'un membre")
    @app_commands.rename(member="membre")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.User) -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return
        await utils.reply(interaction, await self._history_embed(interaction.guild_id, member, True))  # type: ignore[arg-type]

    @app_commands.command(name="history", description="Historique complet des sanctions d'un utilisateur")
    @app_commands.rename(member="utilisateur")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def history(self, interaction: discord.Interaction, member: discord.User) -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return
        await utils.reply(interaction, await self._history_embed(interaction.guild_id, member, False))  # type: ignore[arg-type]

    @app_commands.command(name="case", description="Détail d'une sanction")
    @app_commands.rename(case_id="numero")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def case(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return

        def load():
            with session_scope() as db:
                s = db.get(Sanction, case_id)
                if s is None or s.guild_id != interaction.guild_id:
                    return None
                return (s.type, s.user_id, s.moderator_id, s.reason, s.created_at, s.expires_at, s.active, s.automatic)
        row = await self.bot.db(load)
        if row is None:
            await utils.reply(interaction, utils.err("Sanction introuvable sur ce serveur."))
            return
        kind, uid, mod, reason, at, exp, active, auto = row
        embed = discord.Embed(title=f"{ICONS.get(kind, '')} Sanction n°{case_id} — {LABELS.get(kind, kind)}",
                              color=await self.bot.embed_color(interaction.guild_id))
        embed.add_field(name="Membre", value=f"<@{uid}> (`{uid}`)")
        embed.add_field(name="Par", value=f"<@{mod}>{' (automatique)' if auto else ''}")
        embed.add_field(name="Date", value=utils.ts(at))
        if exp:
            embed.add_field(name="Fin", value=utils.ts(exp))
        embed.add_field(name="Statut", value="active" if active else "terminée / retirée")
        embed.add_field(name="Raison", value=reason or "Aucune raison", inline=False)
        await utils.reply(interaction, embed)

    @app_commands.command(name="clear", description="Supprimer des messages (100 maximum)")
    @app_commands.describe(amount="Nombre de messages à analyser (1-100)", member="Uniquement les messages de ce membre",
                           contains="Uniquement les messages contenant ce texte", bots="Uniquement les messages de bots")
    @app_commands.rename(amount="nombre", member="membre", contains="contient")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                    member: discord.User | None = None, contains: str | None = None, bots: bool = False) -> None:
        if not await self._guard(interaction, "manage_messages", None):
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await utils.reply(interaction, utils.err("Salon non pris en charge."))
            return
        if not channel.permissions_for(interaction.guild.me).manage_messages:  # type: ignore[union-attr]
            await utils.reply(interaction, utils.err("Il me manque la permission « Gérer les messages » ici."))
            return
        await interaction.response.defer(ephemeral=True)
        needle = (contains or "").lower()

        def check(m: discord.Message) -> bool:
            if m.pinned:
                return False
            if member and m.author.id != member.id:
                return False
            if bots and not m.author.bot:
                return False
            return not needle or needle in m.content.lower()
        deleted = await channel.purge(limit=amount, check=check, reason=f"/clear par {interaction.user}")
        await utils.reply(interaction, utils.ok(f"{len(deleted)} message(s) supprimé(s). "
                                                "Les messages de plus de 14 jours et épinglés sont ignorés."))
        await self.bot.send_log(interaction.guild, "moderation", discord.Embed(  # type: ignore[arg-type]
            title="🧹 /clear", description=f"{len(deleted)} messages dans {channel.mention} par {interaction.user.mention}"))

    @app_commands.command(name="slowmode", description="Définir le mode lent du salon (0 pour désactiver)")
    @app_commands.rename(seconds="secondes")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]) -> None:
        if not await self._guard(interaction, "manage_channels", None):
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await utils.reply(interaction, utils.err("Salon non pris en charge."))
            return
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"/slowmode par {interaction.user}")
        await utils.reply(interaction, utils.ok(f"Mode lent : {seconds} s." if seconds else "Mode lent désactivé."),
                          ephemeral=False)

    note = app_commands.Group(name="note", description="Notes internes du staff", guild_only=True,
                              default_permissions=discord.Permissions(moderate_members=True))

    @note.command(name="add", description="Ajouter une note interne sur un membre")
    @app_commands.rename(member="utilisateur", text="note")
    async def note_add(self, interaction: discord.Interaction, member: discord.User,
                       text: app_commands.Range[str, 1, 1500]) -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return

        def save():
            with session_scope() as db:
                db.add(ModNote(guild_id=interaction.guild_id, user_id=member.id, author_id=interaction.user.id,
                               content=text))
        await self.bot.db(save)
        await utils.reply(interaction, utils.ok(f"Note ajoutée pour {member.mention} (visible uniquement par le staff)."))

    @note.command(name="list", description="Notes internes d'un membre")
    @app_commands.rename(member="utilisateur")
    async def note_list(self, interaction: discord.Interaction, member: discord.User) -> None:
        if not await self._guard(interaction, "moderate_members", None):
            return

        def load():
            with session_scope() as db:
                return [(n.id, n.author_id, n.content, n.created_at) for n in db.scalars(
                    select(ModNote).where(ModNote.guild_id == interaction.guild_id, ModNote.user_id == member.id)
                    .order_by(ModNote.id.desc()).limit(15))]
        rows = await self.bot.db(load)
        embed = discord.Embed(title=f"Notes — {member}", color=await self.bot.embed_color(interaction.guild_id),
                              description="\n\n".join(f"{utils.ts(at, 'd')} par <@{a}> :\n{utils.truncate(c, 300)}"
                                                      for _, a, c, at in rows)[:4000] or "Aucune note.")
        await utils.reply(interaction, embed)


ActionType = Literal["warn", "timeout", "kick", "ban"]


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Moderation(bot))
