"""Automatisations configurées dans le Dashboard : déclencheur → conditions → actions.

Garde-fous : liste fermée d'actions (aucun code utilisateur), délai minimum entre deux
exécutions, 30 exécutions par minute et par serveur au maximum, déclencheur « message »
ignoré pour les bots (donc pas de boucle avec les propres messages de Nexbot),
hiérarchie des rôles respectée.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import discord
from discord.ext import commands
from pydantic import ValidationError
from sqlalchemy import select

from ...core import premium as premium_mod
from ...core.automations import ACTIONS, CONDITIONS, TRIGGERS, AutomationDefinition
from ...core.db import session_scope
from ...core.embeds import render_text
from ...core.models import Automation, utcnow
from .. import utils
from ..automod_logic import SlidingCounter, normalize
from ..client import Nexbot

log = logging.getLogger("nexbot.automations")
MAX_RUNS_PER_MINUTE = 30


class Automations(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot
        self._cache: dict[int, list[tuple[int, str, AutomationDefinition]]] = {}
        self._last_change = None
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._rate = SlidingCounter(MAX_RUNS_PER_MINUTE + 5)
        self._tasks: set[asyncio.Task] = set()
        bot.register_job("test_automation", self.job_test)

    async def cog_unload(self) -> None:
        for task in self._tasks:
            task.cancel()

    def notice_change(self, stamp) -> None:
        if stamp != self._last_change:
            self._cache.clear()
            self._last_change = stamp

    async def _load(self, guild_id: int) -> list[tuple[int, str, AutomationDefinition]]:
        if guild_id in self._cache:
            return self._cache[guild_id]
        premium = await self.bot.is_premium(guild_id)
        lim = premium_mod.limits(premium)

        def load():
            with session_scope() as db:
                return [(a.id, a.name, dict(a.definition)) for a in db.scalars(
                    select(Automation).where(Automation.guild_id == guild_id, Automation.enabled.is_(True))
                    .order_by(Automation.id).limit(lim["automations"]))]
        items = []
        for aid, name, raw in await self.bot.db(load):
            try:
                definition = AutomationDefinition.model_validate(raw)
            except ValidationError:
                continue  # revalidation côté bot : une définition invalide n'est jamais exécutée
            definition.actions = definition.actions[: lim["automation_actions"]]
            items.append((aid, name, definition))
        self._cache[guild_id] = items
        return items

    # ------------------------------------------------------------------ évaluation
    @staticmethod
    def _condition(cond, member: discord.Member | None, message: discord.Message | None) -> bool:
        t = cond.type
        if t == "in_channel":
            return message is not None and str(getattr(message.channel, "id", "")) == cond.channel_id
        if member is None:
            return False
        if t == "account_age_below":
            return utils.utcnow() - member.created_at < timedelta(days=cond.days)
        if t == "has_role":
            return any(str(r.id) == cond.role_id for r in member.roles)
        if t == "lacks_role":
            return not any(str(r.id) == cond.role_id for r in member.roles)
        if t == "is_bot":
            return member.bot
        if t == "is_not_bot":
            return not member.bot
        return False

    @staticmethod
    def _trigger_matches(definition: AutomationDefinition, trigger: str, message: discord.Message | None,
                         warn_count: int | None) -> bool:
        tr = definition.trigger
        if tr.type != trigger:
            return False
        if trigger == "message_contains":
            text = normalize(message.content if message else "")
            return any(normalize(k) in text for k in tr.keywords)
        if trigger == "warn_count":
            return warn_count == tr.count
        return True

    async def fire(self, guild: discord.Guild, trigger: str, *, member: discord.Member | None = None,
                   message: discord.Message | None = None, warn_count: int | None = None,
                   extra: dict | None = None) -> int:
        if not self.bot.globally_enabled("automations"):
            return 0
        ran = 0
        for aid, name, definition in await self._load(guild.id):
            if not self._trigger_matches(definition, trigger, message, warn_count):
                continue
            if not all(self._condition(c, member, message) for c in definition.conditions):
                continue
            key = (aid, member.id if member else 0)
            now = time.monotonic()
            if now - self._cooldowns.get(key, 0) < definition.cooldown_seconds:
                continue
            if self._rate.hit(guild.id, 60, now) > MAX_RUNS_PER_MINUTE:
                log.warning("Automatisations limitées sur %s (plus de %d/min)", guild.id, MAX_RUNS_PER_MINUTE)
                return ran
            self._cooldowns[key] = now
            task = asyncio.create_task(self._run(guild, aid, definition, member, message, extra or {}))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            ran += 1
        if len(self._cooldowns) > 50000:
            self._cooldowns.clear()
        return ran

    async def _run(self, guild: discord.Guild, aid: int, definition: AutomationDefinition,
                   member: discord.Member | None, message: discord.Message | None, extra: dict) -> None:
        values = utils.template_values(guild, member, extra)
        for action in definition.actions:
            if action.delay_seconds:
                await asyncio.sleep(action.delay_seconds)
                if member is not None:
                    member = guild.get_member(member.id)  # a pu partir entre-temps
            try:
                await self._execute(guild, action, member, message, values)
            except discord.HTTPException as exc:
                log.info("Action %s de l'automatisation %s impossible : %s", action.type, aid, exc)

        def record():
            with session_scope() as db:
                row = db.get(Automation, aid)
                if row:
                    row.run_count = (row.run_count or 0) + 1
                    row.last_run_at = utcnow()
        await self.bot.db(record)

    async def _execute(self, guild: discord.Guild, action, member: discord.Member | None,
                       message: discord.Message | None, values: dict) -> None:
        me = guild.me
        text = render_text(action.text, values)[:2000] if action.text else ""
        mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
        if action.type == "send_message":
            channel = guild.get_channel(utils.snowflake(action.channel_id) or 0)
            if isinstance(channel, (discord.TextChannel, discord.Thread)) and channel.permissions_for(me).send_messages:
                await channel.send(text, allowed_mentions=mentions)
        elif action.type == "dm_member" and member is not None:
            try:
                await member.send(f"**{guild.name}** : {text}"[:2000])
            except discord.Forbidden:
                pass
        elif action.type in {"add_role", "remove_role"} and member is not None:
            role = guild.get_role(utils.snowflake(action.role_id) or 0)
            if role is None or role.managed or role >= me.top_role:
                return
            if action.type == "add_role" and role not in member.roles:
                await member.add_roles(role, reason="Nexbot : automatisation")
            elif action.type == "remove_role" and role in member.roles:
                await member.remove_roles(role, reason="Nexbot : automatisation")
        elif action.type == "timeout" and member is not None:
            if member.top_role < me.top_role and member.id != guild.owner_id and not member.guild_permissions.administrator:
                await member.timeout(timedelta(minutes=action.minutes), reason="Nexbot : automatisation")
        elif action.type == "delete_message" and message is not None:
            try:
                await message.delete()
            except discord.NotFound:
                pass
        elif action.type == "alert":
            conf = await self.bot.conf(guild.id, "antiraid")
            channel = guild.get_channel(utils.snowflake(conf.alert_channel_id) or 0)
            if isinstance(channel, discord.TextChannel):
                await channel.send(embed=discord.Embed(title="⚡ Alerte d'automatisation", description=text,
                                                       color=utils.WARNING), allowed_mentions=mentions)

    # ------------------------------------------------------------------ événements
    async def _has(self, guild_id: int, trigger: str) -> bool:
        return any(d.trigger.type == trigger for _, _, d in await self._load(guild_id))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if await self._has(member.guild.id, "member_join"):
            await self.fire(member.guild, "member_join", member=member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if await self._has(member.guild.id, "member_leave"):
            await self.fire(member.guild, "member_leave", member=member)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or message.guild is None:
            return
        if not isinstance(message.author, discord.Member):
            return
        if await self._has(message.guild.id, "message_contains"):
            await self.fire(message.guild, "message_contains", member=message.author, message=message)

    # ------------------------------------------------------------------ test depuis le Dashboard (sans effet réel)
    async def job_test(self, job: dict) -> tuple[bool, str]:
        guild: discord.Guild = job["guild"]
        aid = int(job["payload"].get("automation_id", 0))

        def load():
            with session_scope() as db:
                row = db.get(Automation, aid)
                return (row.name, dict(row.definition)) if row and row.guild_id == guild.id else None
        found = await self.bot.db(load)
        if found is None:
            return False, "Automatisation introuvable."
        name, raw = found
        try:
            definition = AutomationDefinition.model_validate(raw)
        except ValidationError:
            return False, "Définition invalide : modifiez puis réenregistrez l'automatisation."
        member = guild.get_member(utils.snowflake(job["payload"].get("member_id")) or 0)
        lines = [f"Simulation de « {name} » avec {member.display_name if member else 'aucun membre'} comme membre :",
                 f"Déclencheur : {TRIGGERS[definition.trigger.type]}"]
        all_ok = True
        for c in definition.conditions:
            ok = self._condition(c, member, None) if c.type != "in_channel" else True
            all_ok &= ok
            lines.append(f"{'✓' if ok else '✗'} {CONDITIONS[c.type]}" + (" (vérifiée sur le vrai message)" if c.type == "in_channel" else ""))
        me = guild.me
        for a in definition.actions:
            problem = ""
            if a.type in {"add_role", "remove_role"}:
                role = guild.get_role(utils.snowflake(a.role_id) or 0)
                problem = "rôle introuvable" if role is None else ("rôle au-dessus de Nexbot" if role >= me.top_role else "")
            elif a.type in {"send_message"}:
                ch = guild.get_channel(utils.snowflake(a.channel_id) or 0)
                problem = "salon introuvable" if ch is None else (
                    "" if ch.permissions_for(me).send_messages else "Nexbot ne peut pas écrire dans ce salon")
            elif a.type == "timeout" and member is not None and member.top_role >= me.top_role:
                problem = "vous êtes au-dessus de Nexbot (sans effet sur vous)"
            delay = f" après {a.delay_seconds} s" if a.delay_seconds else ""
            lines.append(f"{'⚠' if problem else '→'} {ACTIONS[a.type]}{delay}" + (f" — {problem}" if problem else ""))
        lines.append("Résultat : les actions SERAIENT exécutées." if all_ok
                     else "Résultat : conditions non remplies, rien ne serait exécuté.")
        return True, "\n".join(lines)


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Automations(bot))
