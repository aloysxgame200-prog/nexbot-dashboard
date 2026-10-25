"""Tâches de fond : synchronisation avec le Dashboard, jobs, statistiques, nettoyage."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from sqlalchemy import delete, func, select, update

from ...core import jobs as jobs_mod
from ...core import premium as premium_mod
from ...core.config import VERSION
from ...core.db import session_scope
from ...core.models import (ActiveUser, Automation, BotJob, CommandUsage, DailyStat, DashboardAudit, Guild,
                            GuildEvent, GuildSettings, Report, ScheduledMessage, SecurityIncident, Subscription,
                            Ticket, utcnow)
from .. import utils
from ..client import Nexbot

log = logging.getLogger("nexbot.core")


class Core(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot
        self._last_settings_change = None
        self._last_sub_change = None
        bot.register_job("send_message", self.job_send_message)
        bot.register_job("refresh_guild", self.job_refresh_guild)

    async def cog_load(self) -> None:
        for loop in (self.heartbeat, self.watch_changes, self.run_jobs, self.flush_usage, self.scheduled_messages,
                     self.hourly, self.cleanup):
            loop.start()

    async def cog_unload(self) -> None:
        for loop in (self.heartbeat, self.watch_changes, self.run_jobs, self.flush_usage, self.scheduled_messages,
                     self.hourly, self.cleanup):
            loop.cancel()
        await self._flush()

    # ------------------------------------------------------------------ présence des serveurs
    async def _upsert_guild(self, guild: discord.Guild, present: bool = True, event: str | None = None) -> None:
        def save():
            with session_scope() as db:
                row = db.get(Guild, guild.id)
                if row is None:
                    row = Guild(id=guild.id)
                    db.add(row)
                row.name = guild.name[:100]
                row.icon = guild.icon.key if guild.icon else None
                row.owner_id = guild.owner_id
                row.member_count = guild.member_count or 0
                row.bot_present = present
                row.left_at = None if present else utcnow()
                if event == "join":
                    row.joined_at = utcnow()
                if event:
                    db.add(GuildEvent(guild_id=guild.id, kind=event, member_count=guild.member_count or 0))
        await self.bot.db(save)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        current = {g.id for g in self.bot.guilds}
        for guild in self.bot.guilds:
            if guild.id in self.bot.blacklist_guilds:
                await guild.leave()
                continue
            await self._upsert_guild(guild)

        def mark_absent():
            with session_scope() as db:
                db.execute(update(Guild).where(Guild.bot_present.is_(True), Guild.id.not_in(current or {0}))
                           .values(bot_present=False, left_at=utcnow()))
        await self.bot.db(mark_absent)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id in self.bot.blacklist_guilds:
            await guild.leave()
            return
        await self._upsert_guild(guild, event="join")
        log.info("Ajouté sur %s (%s), %s membres", guild.name, guild.id, guild.member_count)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self._upsert_guild(guild, present=False, event="leave")

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name or before.icon != after.icon:
            await self._upsert_guild(after)

    # ------------------------------------------------------------------ boucles
    @tasks.loop(seconds=30)
    async def heartbeat(self) -> None:
        info = {"version": VERSION, "guilds": len(self.bot.guilds),
                "users": sum(g.member_count or 0 for g in self.bot.guilds),
                "latency_ms": round(self.bot.latency * 1000) if self.bot.latency == self.bot.latency else None,
                "maintenance": bool(self.bot.maintenance.get("enabled"))}

        def save():
            with session_scope() as db:
                jobs_mod.heartbeat(db, "bot", info)
        await self.bot.db(save)

    @tasks.loop(seconds=5)
    async def watch_changes(self) -> None:
        """Applique les modifications du Dashboard sans redémarrage."""
        def check():
            with session_scope() as db:
                s = db.scalar(select(func.max(GuildSettings.updated_at)))
                a = db.scalar(select(func.max(Automation.updated_at)))
                p = db.scalar(select(func.max(Subscription.updated_at)))
                return s, a, p
        settings_change, auto_change, sub_change = await self.bot.db(check)
        if self._last_settings_change is not None and settings_change != self._last_settings_change:
            self.bot._settings_cache.clear()
        if self._last_sub_change is not None and sub_change != self._last_sub_change:
            self.bot.invalidate()
        self._last_settings_change, self._last_sub_change = settings_change, sub_change
        automations = self.bot.get_cog("Automations")
        if automations is not None:
            automations.notice_change(auto_change)  # type: ignore[attr-defined]

    @tasks.loop(seconds=3)
    async def run_jobs(self) -> None:
        def claim():
            with session_scope() as db:
                return [(j.id, j.guild_id, j.kind, dict(j.payload or {}), j.created_by)
                        for j in jobs_mod.claim_pending(db)]
        for job_id, guild_id, kind, payload, created_by in await self.bot.db(claim):
            handler = self.bot.job_handlers.get(kind)
            guild = self.bot.get_guild(guild_id)
            if handler is None:
                ok, result = False, "Action non prise en charge par cette version du bot."
            elif guild is None:
                ok, result = False, "Nexbot n'est pas (ou plus) sur ce serveur."
            else:
                try:
                    ok, result = await handler({"guild": guild, "payload": payload, "user_id": created_by})
                except discord.Forbidden:
                    ok, result = False, "Permission refusée par Discord (droits du bot ou position de son rôle)."
                except Exception as exc:  # noqa: BLE001
                    self.bot.capture_error(f"job {kind}", exc, guild)
                    ok, result = False, "Erreur interne pendant l'exécution."

            def finish():
                with session_scope() as db:
                    jobs_mod.finish(db, job_id, ok, result)
            await self.bot.db(finish)

    async def _flush(self) -> None:
        users, cmds, stats = self.bot.usage_users, self.bot.usage_commands, self.bot.stat_buffer
        self.bot.usage_users, self.bot.usage_commands = set(), type(cmds)()
        self.bot.stat_buffer = type(stats)()
        if not (users or cmds or stats):
            return
        today = utcnow().date()

        def save():
            with session_scope() as db:
                existing = set(db.scalars(select(ActiveUser.user_id).where(ActiveUser.day == today,
                                                                           ActiveUser.user_id.in_(users or {0}))))
                for uid in users - existing:
                    db.add(ActiveUser(user_id=uid, day=today))
                for name, n in cmds.items():
                    row = db.get(CommandUsage, {"day": today, "command": name})
                    if row is None:
                        db.add(CommandUsage(day=today, command=name, count=n))
                    else:
                        row.count += n
                for (gid, metric), n in stats.items():
                    jobs_mod.incr(db, gid, metric, n, today)
        await self.bot.db(save)

    @tasks.loop(seconds=60)
    async def flush_usage(self) -> None:
        await self._flush()

    @tasks.loop(seconds=30)
    async def scheduled_messages(self) -> None:
        if not self.bot.globally_enabled("scheduled"):
            return
        now = utcnow()

        def due():
            with session_scope() as db:
                rows = db.scalars(select(ScheduledMessage).where(ScheduledMessage.enabled.is_(True),
                                                                 ScheduledMessage.next_run_at <= now).limit(50)).all()
                return [(r.id, r.guild_id, r.channel_id, dict(r.message), r.interval_minutes) for r in rows]
        for sid, gid, cid, message, interval in await self.bot.db(due):
            guild = self.bot.get_guild(gid)
            sent = False
            if guild is not None and await self.bot.is_premium(gid):
                channel = guild.get_channel(cid)
                if isinstance(channel, discord.abc.Messageable):
                    try:
                        content, embed = utils.build_message(message, utils.template_values(guild))
                        await channel.send(content=content, embed=embed,
                                           allowed_mentions=discord.AllowedMentions.none())
                        sent = True
                    except (discord.HTTPException, ValueError) as exc:
                        log.warning("Annonce %s non envoyée : %s", sid, exc)

            def reschedule():
                with session_scope() as db:
                    row = db.get(ScheduledMessage, sid)
                    if row is None:
                        return
                    if sent:
                        row.last_run_at = now
                    if interval and interval >= 30:
                        nxt = row.next_run_at if row.next_run_at.tzinfo else row.next_run_at.replace(tzinfo=now.tzinfo)
                        while nxt <= now:  # pas de rafale de rattrapage après une coupure
                            nxt += timedelta(minutes=interval)
                        row.next_run_at = nxt
                    else:
                        row.enabled = False  # envoi unique (ou serveur non Premium : mise en pause)
            await self.bot.db(reschedule)

    @tasks.loop(hours=1)
    async def hourly(self) -> None:
        def expire():
            with session_scope() as db:
                return premium_mod.expire_overdue(db)
        if await self.bot.db(expire):
            self.bot.invalidate()

    @tasks.loop(hours=6)
    async def cleanup(self) -> None:
        """Conservation limitée des données (durée réglée par serveur dans « Logs »)."""
        now = utcnow()
        guild_ids = [g.id for g in self.bot.guilds]
        retention = {}
        for gid in guild_ids:
            retention[gid] = (await self.bot.conf(gid, "logs")).retention_days

        def purge():
            with session_scope() as db:
                for gid, days in retention.items():
                    limit = now - timedelta(days=days)
                    db.execute(delete(SecurityIncident).where(SecurityIncident.guild_id == gid,
                                                              SecurityIncident.created_at < limit,
                                                              SecurityIncident.kind != "quarantine"))
                    db.execute(delete(Report).where(Report.guild_id == gid, Report.created_at < limit,
                                                    Report.status != "open"))
                    db.execute(delete(Ticket).where(Ticket.guild_id == gid, Ticket.status == "closed",
                                                    Ticket.closed_at < limit))
                db.execute(delete(ActiveUser).where(ActiveUser.day < (now - timedelta(days=90)).date()))
                db.execute(delete(BotJob).where(BotJob.created_at < now - timedelta(days=7)))
                db.execute(delete(DashboardAudit).where(DashboardAudit.created_at < now - timedelta(days=180)))
                db.execute(delete(DailyStat).where(DailyStat.day < (now - timedelta(days=400)).date()))
        await self.bot.db(purge)

    @heartbeat.before_loop
    @run_jobs.before_loop
    @scheduled_messages.before_loop
    @cleanup.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ jobs
    async def job_send_message(self, job: dict) -> tuple[bool, str]:
        guild: discord.Guild = job["guild"]
        if not await self.bot.is_premium(guild.id):
            return False, "L'envoi depuis l'éditeur est réservé au Premium."
        channel = guild.get_channel(utils.snowflake(job["payload"].get("channel_id")) or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return False, "Salon introuvable."
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return False, f"Il me manque « Envoyer des messages » ou « Intégrer des liens » dans #{channel.name}."
        content, embed = utils.build_message(job["payload"]["message"], utils.template_values(guild))
        await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True, f"Message envoyé dans #{channel.name}."

    async def job_refresh_guild(self, job: dict) -> tuple[bool, str]:
        await self._upsert_guild(job["guild"])
        self.bot.invalidate(job["guild"].id)
        return True, "Serveur resynchronisé."


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Core(bot))
