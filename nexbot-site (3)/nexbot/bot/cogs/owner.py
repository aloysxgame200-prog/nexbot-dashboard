"""Commandes globales `*`, réservées EXCLUSIVEMENT au propriétaire de Nexbot (ID 598080343958093831).

Trois verrous indépendants :
1. Nexbot.on_message ne traite un message `*` que si l'auteur est le propriétaire ;
2. un check global du bot refuse toute commande à préfixe aux autres ;
3. cog_check ci-dessous.
Pour tout autre utilisateur : aucune réponse, aucune réaction, aucun effet de bord.
"""
from __future__ import annotations

import platform
import re
from datetime import timedelta

import discord
from discord.ext import commands
from sqlalchemy import distinct, func, select

from ...core.config import VERSION, get_settings
from ...core.db import session_scope
from ...core.models import (ActiveUser, Automation, BotJob, CommandUsage, GlobalBlacklist, Guild, GuildEvent,
                            Sanction, SecurityIncident, Subscription, Ticket, WebSession, utcnow)
from ...core.premium import ACTIVE_STATUSES, GRACE_PERIOD, subscription_is_active
from ...core.security import OWNER_ID, is_owner
from .. import utils
from ..client import GLOBAL_MODULES, Nexbot

STREAM_URL = re.compile(r"^https://(www\.)?(twitch\.tv|youtube\.com|youtu\.be)/\S+$")


class Confirm(discord.ui.View):
    """Confirmation par boutons, utilisable uniquement par le propriétaire."""

    def __init__(self) -> None:
        super().__init__(timeout=30)
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return is_owner(interaction.user.id)

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.edit_message(embed=utils.warn_embed("Action annulée."), view=None)
        self.stop()


HELP: list[tuple[str, list[tuple[str, str]]]] = [
    ("📊 Statistiques", [
        ("*stats", "Combien de serveurs, de membres et d'utilisateurs actifs utilisent Nexbot"),
        ("*growth [jours]", "Arrivées / départs de Nexbot sur des serveurs, jour par jour"),
        ("*cmdstats [jours]", "Classement des commandes les plus utilisées"),
        ("*globalinfo", "Version, uptime, latence, base de données, état des modules"),
        ("*guilds [page]", "Liste des serveurs (triés par nombre de membres)"),
        ("*guildinfo <id>", "Détails d'un serveur"),
    ]),
    ("🎛️ Présence", [
        ("*status [online|idle|dnd|invisible]", "Voir ou changer le statut"),
        ("*activity <texte>", "Statut personnalisé"),
        ("*playing / *watching / *listening / *competing <texte>", "Type d'activité"),
        ("*streaming <url twitch/youtube> <texte>", "Activité « En direct »"),
        ("*presence reset", "Présence par défaut"),
    ]),
    ("🛠️ Administration", [
        ("*maintenance on [message] / off", "Mode maintenance (commandes publiques suspendues)"),
        ("*module <nom> on|off · *modules", "Couper / réactiver un module partout"),
        ("*premium grant <serveur> <jours> · revoke · list · info", "Gérer le Premium manuellement"),
        ("*blacklist user|guild <id> [raison] · remove <id> · list", "Interdire Nexbot à un utilisateur / serveur"),
        ("*leave <id serveur>", "Faire quitter un serveur à Nexbot"),
    ]),
    ("⚙️ Technique", [
        ("*sync [here|global|clear]", "Synchroniser les commandes slash"),
        ("*reloadconfig", "Recharger l'état global et vider les caches"),
        ("*reload [module|all]", "Recharger le code d'un module du bot"),
        ("*jobs · *db · *errors", "File d'actions du Dashboard, base de données, dernières erreurs"),
        ("*ping · *uptime · *invite", "Latence, durée de fonctionnement, lien d'invitation"),
        ("*shutdown · *restart", "Arrêt propre / redémarrage contrôlé"),
    ]),
]


class Owner(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:  # type: ignore[override]
        return is_owner(ctx.author.id)

    async def _confirm(self, ctx: commands.Context, text: str) -> bool:
        view = Confirm()
        msg = await ctx.reply(embed=utils.warn_embed(text), view=view, mention_author=False)
        await view.wait()
        if view.value is None:
            await msg.edit(embed=utils.warn_embed("Délai dépassé : action annulée."), view=None)
        return bool(view.value)

    async def _done(self, ctx: commands.Context, text: str) -> None:
        await ctx.reply(embed=utils.ok(text), mention_author=False)

    # ================================================================== aide
    @commands.command(name="help", aliases=["aide"])
    async def help_(self, ctx: commands.Context) -> None:
        embed = utils.nexbot_embed(self.bot, "Commandes propriétaire", "Visibles et utilisables uniquement par toi.")
        for title, items in HELP:
            embed.add_field(name=title, value="\n".join(f"`{c}` — {d}" for c, d in items), inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    # ================================================================== statistiques
    def _usage_numbers(self) -> dict:
        now = utcnow()
        today = now.date()
        with session_scope() as db:
            def active(days: int) -> int:
                return db.scalar(select(func.count(distinct(ActiveUser.user_id)))
                                 .where(ActiveUser.day > today - timedelta(days=days))) or 0

            def cmds(days: int) -> int:
                return db.scalar(select(func.coalesce(func.sum(CommandUsage.count), 0))
                                 .where(CommandUsage.day > today - timedelta(days=days))) or 0

            def guild_events(kind: str, days: int) -> int:
                return db.scalar(select(func.count()).select_from(GuildEvent)
                                 .where(GuildEvent.kind == kind, GuildEvent.created_at >= now - timedelta(days=days))) or 0

            top = db.execute(select(CommandUsage.command, func.sum(CommandUsage.count).label("n"))
                             .where(CommandUsage.day > today - timedelta(days=30))
                             .group_by(CommandUsage.command).order_by(func.sum(CommandUsage.count).desc()).limit(5)).all()
            subs = db.scalars(select(Subscription).where(Subscription.status.in_(ACTIVE_STATUSES))).all()
            premium = len({s.guild_id for s in subs if subscription_is_active(s, now)})
            dash_users = db.scalar(select(func.count(distinct(WebSession.user_id)))
                                   .where(WebSession.expires_at > now)) or 0
            return {
                "dau": active(1), "wau": active(7), "mau": active(30),
                "cmd_day": cmds(1), "cmd_week": cmds(7), "cmd_month": cmds(30), "top": top,
                "join7": guild_events("join", 7), "leave7": guild_events("leave", 7),
                "join30": guild_events("join", 30), "leave30": guild_events("leave", 30),
                "premium": premium, "dash_users": dash_users,
                "sanctions30": db.scalar(select(func.count()).select_from(Sanction)
                                         .where(Sanction.created_at >= now - timedelta(days=30))) or 0,
                "incidents30": db.scalar(select(func.count()).select_from(SecurityIncident)
                                         .where(SecurityIncident.created_at >= now - timedelta(days=30))) or 0,
            }

    @commands.command(name="stats", aliases=["usage", "users", "utilisateurs"])
    async def stats(self, ctx: commands.Context) -> None:
        """Combien de personnes utilisent Nexbot."""
        await self.bot.cogs["Core"]._flush()  # type: ignore[attr-defined]  # chiffres à la seconde près
        guilds = self.bot.guilds
        total_members = sum(g.member_count or 0 for g in guilds)
        unique_humans = len({m.id for g in guilds for m in g.members if not m.bot})
        biggest = max(guilds, key=lambda g: g.member_count or 0, default=None)
        n = await self.bot.db(self._usage_numbers)
        fmt = lambda x: f"{x:,}".replace(",", " ")  # noqa: E731

        embed = utils.nexbot_embed(self.bot, "📊 Utilisation de Nexbot")
        embed.add_field(name="Portée", inline=False, value=(
            f"**{fmt(len(guilds))}** serveurs\n"
            f"**{fmt(total_members)}** membres au total (somme des serveurs)\n"
            f"**{fmt(unique_humans)}** personnes uniques (hors bots, membres en cache)\n"
            + (f"Plus grand serveur : **{biggest.name}** ({fmt(biggest.member_count or 0)})" if biggest else "")))
        embed.add_field(name="Utilisateurs actifs", inline=True, value=(
            f"Aujourd'hui : **{fmt(n['dau'])}**\n7 jours : **{fmt(n['wau'])}**\n30 jours : **{fmt(n['mau'])}**"))
        embed.add_field(name="Commandes utilisées", inline=True, value=(
            f"Aujourd'hui : **{fmt(n['cmd_day'])}**\n7 jours : **{fmt(n['cmd_week'])}**\n30 jours : **{fmt(n['cmd_month'])}**"))
        embed.add_field(name="Croissance", inline=True, value=(
            f"7 j : **+{n['join7']}** / **−{n['leave7']}**\n30 j : **+{n['join30']}** / **−{n['leave30']}**"))
        embed.add_field(name="Offre & Dashboard", inline=True, value=(
            f"Serveurs Premium : **{n['premium']}**\nConnectés au Dashboard : **{n['dash_users']}**"))
        embed.add_field(name="Protection (30 j)", inline=True, value=(
            f"Sanctions : **{fmt(n['sanctions30'])}**\nIncidents détectés : **{fmt(n['incidents30'])}**"))
        if n["top"]:
            embed.add_field(name="Top commandes (30 j)", inline=False,
                            value="\n".join(f"`/{c}` — {fmt(int(k))}" for c, k in n["top"]))
        embed.description = ("Un utilisateur actif = une personne qui a utilisé une commande, un bouton ou un "
                             "ticket de Nexbot. Aucun contenu de message n'est enregistré pour ces chiffres.")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="growth", aliases=["croissance"])
    async def growth(self, ctx: commands.Context, days: int = 14) -> None:
        days = max(1, min(days, 60))
        start = utcnow() - timedelta(days=days - 1)

        def load():
            with session_scope() as db:
                return db.execute(select(GuildEvent.kind, GuildEvent.created_at)
                                  .where(GuildEvent.created_at >= start.replace(hour=0, minute=0, second=0))).all()
        rows = await self.bot.db(load)
        per_day: dict[str, list[int]] = {}
        for i in range(days):
            per_day[(start + timedelta(days=i)).strftime("%d/%m")] = [0, 0]
        for kind, at in rows:
            key = at.strftime("%d/%m")
            if key in per_day:
                per_day[key][0 if kind == "join" else 1] += 1
        lines = [f"`{d}`  +{j:<3} −{lv:<3} {'🟩' * min(j, 15)}{'🟥' * min(lv, 15)}" for d, (j, lv) in per_day.items()]
        embed = utils.nexbot_embed(self.bot, f"📈 Croissance sur {days} jours", "\n".join(lines))
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="cmdstats")
    async def cmdstats(self, ctx: commands.Context, days: int = 30) -> None:
        days = max(1, min(days, 365))

        def load():
            with session_scope() as db:
                return db.execute(select(CommandUsage.command, func.sum(CommandUsage.count))
                                  .where(CommandUsage.day > utcnow().date() - timedelta(days=days))
                                  .group_by(CommandUsage.command).order_by(func.sum(CommandUsage.count).desc())).all()
        rows = await self.bot.db(load)
        lines = [f"`{i + 1:>2}.` /{c} — **{int(n)}**" for i, (c, n) in enumerate(rows[:40])] or ["Aucune donnée."]
        await ctx.reply(embed=utils.nexbot_embed(self.bot, f"Commandes ({days} j)", "\n".join(lines)),
                        mention_author=False)

    @commands.command(name="globalinfo", aliases=["botinfo", "info"])
    async def globalinfo(self, ctx: commands.Context) -> None:
        s = get_settings()
        uptime = utcnow() - self.bot.started_at

        def load():
            with session_scope() as db:
                pending = db.scalar(select(func.count()).select_from(BotJob).where(BotJob.status == "pending")) or 0
                autos = db.scalar(select(func.count()).select_from(Automation).where(Automation.enabled.is_(True))) or 0
                tickets = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "open")) or 0
            return pending, autos, tickets
        pending, autos, tickets = await self.bot.db(load)
        embed = utils.nexbot_embed(self.bot, "Nexbot — informations générales")
        embed.add_field(name="Bot", value=(f"Version **{VERSION}**\nUptime : {utils.fmt_duration(uptime)}\n"
                                           f"Latence : {round(self.bot.latency * 1000)} ms\n"
                                           f"Shards : {self.bot.shard_count or 1}"))
        embed.add_field(name="Environnement", value=(f"Python {platform.python_version()}\n"
                                                     f"discord.py {discord.__version__}\n"
                                                     f"Base : {'PostgreSQL' if 'postgresql' in s.database_url else 'SQLite'}\n"
                                                     f"Mode : {s.env}"))
        embed.add_field(name="Activité", value=(f"Serveurs : {len(self.bot.guilds)}\n"
                                                f"Tickets ouverts : {tickets}\nAutomatisations actives : {autos}\n"
                                                f"Actions Dashboard en attente : {pending}"))
        embed.add_field(name="État global", inline=False, value=(
            f"Maintenance : **{'ACTIVE' if self.bot.maintenance.get('enabled') else 'non'}**\n"
            f"Modules coupés : {', '.join(sorted(self.bot.disabled_modules)) or 'aucun'}\n"
            f"Blacklist : {len(self.bot.blacklist_users)} utilisateur(s), {len(self.bot.blacklist_guilds)} serveur(s)\n"
            f"Paiements Stripe : {'configurés' if s.stripe_enabled else 'non configurés'}\n"
            f"Dashboard : {s.dashboard_url if 'localhost' not in s.dashboard_url else 'URL publique par défaut'}"))
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="guilds", aliases=["servers", "serveurs"])
    async def guilds(self, ctx: commands.Context, page: int = 1) -> None:
        per = 15
        ordered = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        pages = max(1, -(-len(ordered) // per))
        page = max(1, min(page, pages))
        premium = {g.id for g in ordered[(page - 1) * per: page * per] if await self.bot.is_premium(g.id)}
        lines = [f"`{i}.` **{utils.truncate(g.name, 40)}** — {g.member_count or 0} membres · `{g.id}`"
                 + (" ✦" if g.id in premium else "")
                 for i, g in enumerate(ordered[(page - 1) * per: page * per], start=(page - 1) * per + 1)]
        embed = utils.nexbot_embed(self.bot, f"Serveurs ({len(ordered)})", "\n".join(lines) or "Aucun serveur.")
        embed.set_footer(text=f"Page {page}/{pages} · *guilds <page> · ✦ = Premium")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="guildinfo", aliases=["serverinfo"])
    async def guildinfo(self, ctx: commands.Context, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)

        def load():
            with session_scope() as db:
                row = db.get(Guild, guild_id)
                sanctions = db.scalar(select(func.count()).select_from(Sanction).where(Sanction.guild_id == guild_id))
                incidents = db.scalar(select(func.count()).select_from(SecurityIncident)
                                      .where(SecurityIncident.guild_id == guild_id))
                return row, sanctions, incidents
        row, sanctions, incidents = await self.bot.db(load)
        if guild is None and row is None:
            await ctx.reply(embed=utils.err("Serveur inconnu."), mention_author=False)
            return
        embed = utils.nexbot_embed(self.bot, guild.name if guild else (row.name if row else str(guild_id)))
        if guild:
            if guild.icon:
                embed.set_thumbnail(url=guild.icon.url)
            embed.add_field(name="Serveur", value=(f"ID : `{guild.id}`\nPropriétaire : <@{guild.owner_id}> (`{guild.owner_id}`)\n"
                                                   f"Créé : {utils.ts(guild.created_at, 'D')}\nMembres : {guild.member_count}\n"
                                                   f"Salons : {len(guild.channels)} · Rôles : {len(guild.roles)}"))
            me = guild.me
            embed.add_field(name="Nexbot", value=(f"Arrivé : {utils.ts(me.joined_at, 'R')}\n"
                                                  f"Rôle le plus haut : {me.top_role.name} (position {me.top_role.position}/{len(guild.roles) - 1})\n"
                                                  f"Administrateur : {'oui' if me.guild_permissions.administrator else 'non'}"))
        else:
            embed.description = "Nexbot n'est plus sur ce serveur."
        embed.add_field(name="Données", inline=False, value=(f"Premium : {'oui' if await self.bot.is_premium(guild_id) else 'non'}\n"
                                                             f"Sanctions : {sanctions} · Incidents : {incidents}\n"
                                                             f"Blacklisté : {'oui' if guild_id in self.bot.blacklist_guilds else 'non'}"))
        await ctx.reply(embed=embed, mention_author=False)

    # ================================================================== présence
    async def _set_presence(self, ctx: commands.Context, **changes) -> None:
        self.bot.presence_conf.update(changes)
        await self.bot.save_global_state("presence", self.bot.presence_conf)
        await self.bot.apply_presence()
        conf = self.bot.presence_conf
        await self._done(ctx, f"Présence : **{conf['status']}** · {conf['type']} « {conf['text'] or '—'} »")

    @commands.command(name="status")
    async def status(self, ctx: commands.Context, value: str | None = None) -> None:
        if value is None:
            conf = self.bot.presence_conf
            await ctx.reply(embed=utils.nexbot_embed(self.bot, "Statut actuel", (
                f"Statut : **{conf['status']}**\nActivité : {conf['type']} « {conf['text'] or '—'} »\n"
                f"Maintenance : {'active' if self.bot.maintenance.get('enabled') else 'non'}\n"
                f"Latence : {round(self.bot.latency * 1000)} ms")), mention_author=False)
            return
        value = value.lower()
        if value not in {"online", "idle", "dnd", "invisible"}:
            await ctx.reply(embed=utils.err("Valeurs possibles : online, idle, dnd, invisible."), mention_author=False)
            return
        await self._set_presence(ctx, status=value)

    @commands.command(name="activity", aliases=["custom"])
    async def activity(self, ctx: commands.Context, *, text: str) -> None:
        await self._set_presence(ctx, type="custom", text=text[:128], url="")

    @commands.command(name="playing")
    async def playing(self, ctx: commands.Context, *, text: str) -> None:
        await self._set_presence(ctx, type="playing", text=text[:128], url="")

    @commands.command(name="watching")
    async def watching(self, ctx: commands.Context, *, text: str) -> None:
        await self._set_presence(ctx, type="watching", text=text[:128], url="")

    @commands.command(name="listening")
    async def listening(self, ctx: commands.Context, *, text: str) -> None:
        await self._set_presence(ctx, type="listening", text=text[:128], url="")

    @commands.command(name="competing")
    async def competing(self, ctx: commands.Context, *, text: str) -> None:
        await self._set_presence(ctx, type="competing", text=text[:128], url="")

    @commands.command(name="streaming")
    async def streaming(self, ctx: commands.Context, url: str, *, text: str) -> None:
        if not STREAM_URL.match(url):
            await ctx.reply(embed=utils.err("Discord n'affiche « En direct » que pour une URL Twitch ou YouTube."),
                            mention_author=False)
            return
        await self._set_presence(ctx, type="streaming", text=text[:128], url=url)

    @commands.command(name="presence")
    async def presence(self, ctx: commands.Context, action: str = "reset") -> None:
        from ..client import DEFAULT_PRESENCE

        if action.lower() != "reset":
            await ctx.reply(embed=utils.err("Utilisation : `*presence reset`"), mention_author=False)
            return
        self.bot.presence_conf = dict(DEFAULT_PRESENCE)
        await self._set_presence(ctx)

    # ================================================================== administration
    @commands.command(name="maintenance")
    async def maintenance(self, ctx: commands.Context, state: str, *, message: str = "") -> None:
        state = state.lower()
        if state not in {"on", "off"}:
            await ctx.reply(embed=utils.err("Utilisation : `*maintenance on [message]` ou `*maintenance off`"),
                            mention_author=False)
            return
        self.bot.maintenance = {"enabled": state == "on", "message": message[:300] if state == "on" else ""}
        await self.bot.save_global_state("maintenance", self.bot.maintenance)
        await self.bot.apply_presence()
        await self._done(ctx, "Maintenance **activée** : les commandes publiques sont suspendues, la protection "
                              "(anti-raid, filtres, logs) continue de fonctionner." if state == "on"
                         else "Maintenance **désactivée**.")

    @commands.command(name="module")
    async def module(self, ctx: commands.Context, name: str, state: str) -> None:
        name, state = name.lower(), state.lower()
        if name not in GLOBAL_MODULES or state not in {"on", "off"}:
            await ctx.reply(embed=utils.err(f"Modules : {', '.join(GLOBAL_MODULES)} · état : on/off"),
                            mention_author=False)
            return
        if name == "antiraid" and state == "off" and not await self._confirm(
                ctx, "Couper l'anti-raid sur **tous** les serveurs ? Les serveurs ne seront plus protégés."):
            return
        (self.bot.disabled_modules.discard if state == "on" else self.bot.disabled_modules.add)(name)
        await self.bot.save_global_state("modules", {"disabled": sorted(self.bot.disabled_modules)})
        await self._done(ctx, f"Module **{name}** {'réactivé' if state == 'on' else 'coupé'} sur tous les serveurs.")

    @commands.command(name="modules")
    async def modules(self, ctx: commands.Context) -> None:
        lines = [f"{'🔴' if m in self.bot.disabled_modules else '🟢'} `{m}`" for m in GLOBAL_MODULES]
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Modules (global)", "\n".join(lines)), mention_author=False)

    @commands.group(name="premium", invoke_without_command=True)
    async def premium(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Premium", (
            "`*premium grant <id serveur> <jours>` — offrir / prolonger le Premium\n"
            "`*premium revoke <id serveur>` — retirer le Premium offert\n"
            "`*premium list` — serveurs Premium actifs\n`*premium info <id serveur>`")), mention_author=False)

    @premium.command(name="grant", aliases=["give"])
    async def premium_grant(self, ctx: commands.Context, guild_id: int, days: int) -> None:
        if not 1 <= days <= 3650:
            await ctx.reply(embed=utils.err("Durée : entre 1 et 3650 jours."), mention_author=False)
            return

        def grant():
            with session_scope() as db:
                current = db.scalars(select(Subscription).where(Subscription.guild_id == guild_id,
                                                                Subscription.source == "manual",
                                                                Subscription.status == "active")).first()
                base = utcnow()
                if current and current.current_period_end and current.current_period_end.replace(
                        tzinfo=base.tzinfo) > base:
                    base = current.current_period_end.replace(tzinfo=base.tzinfo)
                if current is None:
                    current = Subscription(guild_id=guild_id, purchaser_id=OWNER_ID, source="manual", status="active")
                    db.add(current)
                current.current_period_end = base + timedelta(days=days)
                return current.current_period_end
        end = await self.bot.db(grant)
        self.bot.invalidate(guild_id)
        await self._done(ctx, f"Premium offert à `{guild_id}` jusqu'au {utils.ts(end, 'D')}.")

    @premium.command(name="revoke", aliases=["remove"])
    async def premium_revoke(self, ctx: commands.Context, guild_id: int) -> None:
        def revoke():
            with session_scope() as db:
                n = 0
                for sub in db.scalars(select(Subscription).where(Subscription.guild_id == guild_id,
                                                                 Subscription.source.in_(["manual", "dev"]))):
                    sub.status = "expired"
                    sub.current_period_end = utcnow() - GRACE_PERIOD - timedelta(seconds=1)
                    n += 1
                return n
        n = await self.bot.db(revoke)
        self.bot.invalidate(guild_id)
        await self._done(ctx, f"{n} abonnement(s) offert(s) retiré(s). Les abonnements Stripe ne sont pas modifiés "
                              "(gérés par Stripe). Les personnalisations du serveur sont conservées.")

    @premium.command(name="list")
    async def premium_list(self, ctx: commands.Context) -> None:
        def load():
            with session_scope() as db:
                subs = db.scalars(select(Subscription).where(Subscription.status.in_(ACTIVE_STATUSES))).all()
                return [(s.guild_id, s.source, s.current_period_end) for s in subs if subscription_is_active(s)]
        rows = await self.bot.db(load)
        lines = []
        for gid, source, end in rows[:40]:
            g = self.bot.get_guild(gid)
            lines.append(f"**{utils.truncate(g.name, 30) if g else gid}** · {source} · fin {utils.ts(end, 'D')}")
        await ctx.reply(embed=utils.nexbot_embed(self.bot, f"Serveurs Premium ({len(rows)})",
                                                 "\n".join(lines) or "Aucun."), mention_author=False)

    @premium.command(name="info")
    async def premium_info(self, ctx: commands.Context, guild_id: int) -> None:
        def load():
            with session_scope() as db:
                return [(s.source, s.status, s.current_period_end, s.cancel_at_period_end)
                        for s in db.scalars(select(Subscription).where(Subscription.guild_id == guild_id)
                                            .order_by(Subscription.id.desc()).limit(10))]
        rows = await self.bot.db(load)
        lines = [f"{src} · **{st}** · fin {utils.ts(end, 'D')}{' · annulation prévue' if cancel else ''}"
                 for src, st, end, cancel in rows]
        await ctx.reply(embed=utils.nexbot_embed(self.bot, f"Premium de {guild_id}",
                                                 f"Actif : **{'oui' if await self.bot.is_premium(guild_id) else 'non'}**\n"
                                                 + ("\n".join(lines) or "Aucun abonnement.")), mention_author=False)

    @commands.group(name="blacklist", aliases=["bl"], invoke_without_command=True)
    async def blacklist(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Blacklist", (
            "`*blacklist user <id> [raison]`\n`*blacklist guild <id> [raison]` (Nexbot quitte le serveur)\n"
            "`*blacklist remove <id>`\n`*blacklist list`")), mention_author=False)

    async def _bl_add(self, ctx: commands.Context, target_id: int, kind: str, reason: str) -> None:
        if is_owner(target_id):
            await ctx.reply(embed=utils.err("Impossible de te blacklister toi-même."), mention_author=False)
            return

        def save():
            with session_scope() as db:
                row = db.get(GlobalBlacklist, target_id)
                if row is None:
                    db.add(GlobalBlacklist(target_id=target_id, kind=kind, reason=reason[:500]))
                else:
                    row.kind, row.reason = kind, reason[:500]
        await self.bot.db(save)
        (self.bot.blacklist_users if kind == "user" else self.bot.blacklist_guilds).add(target_id)
        extra = ""
        if kind == "guild" and (g := self.bot.get_guild(target_id)):
            await g.leave()
            extra = f" Nexbot a quitté **{g.name}**."
        await self._done(ctx, f"`{target_id}` ajouté à la blacklist ({kind}).{extra}")

    @blacklist.command(name="user")
    async def bl_user(self, ctx: commands.Context, user_id: int, *, reason: str = "") -> None:
        await self._bl_add(ctx, user_id, "user", reason)

    @blacklist.command(name="guild", aliases=["server"])
    async def bl_guild(self, ctx: commands.Context, guild_id: int, *, reason: str = "") -> None:
        await self._bl_add(ctx, guild_id, "guild", reason)

    @blacklist.command(name="remove", aliases=["del"])
    async def bl_remove(self, ctx: commands.Context, target_id: int) -> None:
        def delete_row():
            with session_scope() as db:
                row = db.get(GlobalBlacklist, target_id)
                if row:
                    db.delete(row)
                return row is not None
        found = await self.bot.db(delete_row)
        self.bot.blacklist_users.discard(target_id)
        self.bot.blacklist_guilds.discard(target_id)
        await ctx.reply(embed=utils.ok(f"`{target_id}` retiré de la blacklist.") if found
                        else utils.err("Cet ID n'est pas blacklisté."), mention_author=False)

    @blacklist.command(name="list")
    async def bl_list(self, ctx: commands.Context) -> None:
        def load():
            with session_scope() as db:
                return [(b.target_id, b.kind, b.reason) for b in db.query(GlobalBlacklist).limit(50)]
        rows = await self.bot.db(load)
        lines = [f"`{t}` · {k}{' · ' + utils.truncate(r, 60) if r else ''}" for t, k, r in rows]
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Blacklist", "\n".join(lines) or "Vide."),
                        mention_author=False)

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.reply(embed=utils.err("Nexbot n'est pas sur ce serveur."), mention_author=False)
            return
        if await self._confirm(ctx, f"Quitter **{guild.name}** ({guild.member_count} membres) ?"):
            await guild.leave()
            await self._done(ctx, f"Nexbot a quitté **{guild.name}**.")

    # ================================================================== technique
    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context, scope: str = "global") -> None:
        scope = scope.lower()
        if scope == "here" and ctx.guild:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
            await self._done(ctx, f"{len(synced)} commandes synchronisées sur ce serveur (immédiat).")
        elif scope == "clear" and ctx.guild:
            self.bot.tree.clear_commands(guild=ctx.guild)
            await self.bot.tree.sync(guild=ctx.guild)
            await self._done(ctx, "Commandes spécifiques à ce serveur supprimées (les globales restent).")
        else:
            synced = await self.bot.tree.sync()
            await self._done(ctx, f"{len(synced)} commandes synchronisées globalement "
                                  "(Discord peut mettre jusqu'à une heure à les afficher partout).")

    @commands.command(name="reloadconfig")
    async def reloadconfig(self, ctx: commands.Context) -> None:
        await self.bot.load_global_state()
        self.bot.invalidate()
        await self.bot.apply_presence()
        await self._done(ctx, "État global rechargé et caches vidés : les paramètres de tous les serveurs sont relus.")

    @commands.command(name="reload")
    async def reload(self, ctx: commands.Context, name: str = "all") -> None:
        from ..client import EXTENSIONS

        targets = EXTENSIONS if name == "all" else [e for e in EXTENSIONS if e.endswith("." + name)]
        if not targets:
            await ctx.reply(embed=utils.err("Module inconnu. Ex. : `*reload moderation`"), mention_author=False)
            return
        failed = []
        for ext in targets:
            try:
                await self.bot.reload_extension(ext)
            except commands.ExtensionError as exc:
                failed.append(f"{ext} : {exc}")
        await ctx.reply(embed=utils.ok(f"{len(targets) - len(failed)} module(s) rechargé(s).") if not failed
                        else utils.err("\n".join(failed)[:3900]), mention_author=False)

    @commands.command(name="jobs")
    async def jobs(self, ctx: commands.Context) -> None:
        def load():
            with session_scope() as db:
                since = utcnow() - timedelta(hours=24)
                return db.execute(select(BotJob.status, func.count()).where(BotJob.created_at >= since)
                                  .group_by(BotJob.status)).all()
        rows = dict(await self.bot.db(load))
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Actions du Dashboard (24 h)", (
            f"En attente : {rows.get('pending', 0)}\nEn cours : {rows.get('running', 0)}\n"
            f"Terminées : {rows.get('done', 0)}\nÉchouées : {rows.get('failed', 0)}")), mention_author=False)

    @commands.command(name="db")
    async def db_stats(self, ctx: commands.Context) -> None:
        from ...core.db import Base

        def load():
            with session_scope() as db:
                return {t.name: db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}
        counts = await self.bot.db(load)
        lines = [f"`{k}` : {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Base de données", "\n".join(lines)), mention_author=False)

    @commands.command(name="errors")
    async def errors(self, ctx: commands.Context) -> None:
        items = list(self.bot.recent_errors)[-10:]
        lines = [f"{utils.ts(e['at'], 'R')} **{e['where']}**{' · ' + str(e['guild']) if e['guild'] else ''}\n"
                 f"`{utils.truncate(e['error'], 250)}`" for e in reversed(items)]
        await ctx.reply(embed=utils.nexbot_embed(self.bot, "Dernières erreurs", "\n".join(lines)[:4000] or "Aucune 🎉"),
                        mention_author=False)

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        await self._done(ctx, f"Pong ! {round(self.bot.latency * 1000)} ms")

    @commands.command(name="uptime")
    async def uptime(self, ctx: commands.Context) -> None:
        await self._done(ctx, f"En ligne depuis {utils.ts(self.bot.started_at, 'R')} "
                              f"({utils.fmt_duration(utcnow() - self.bot.started_at)}).")

    @commands.command(name="invite")
    async def invite(self, ctx: commands.Context) -> None:
        await self._done(ctx, f"[Lien d'invitation]({get_settings().bot_invite_url()})")

    @commands.command(name="shutdown", aliases=["stop"])
    async def shutdown(self, ctx: commands.Context) -> None:
        if await self._confirm(ctx, "Arrêter Nexbot ? Il restera hors ligne jusqu'à un relancement manuel."):
            await self._done(ctx, "Arrêt en cours… 👋")
            await self.bot.close()

    @commands.command(name="restart", aliases=["reboot"])
    async def restart(self, ctx: commands.Context) -> None:
        if await self._confirm(ctx, "Redémarrer Nexbot ? Fonctionne si le bot est lancé avec `run_bot.sh` / "
                                    "`run_bot.bat` ou un hébergeur qui relance automatiquement le processus."):
            self.bot.restart_requested = True  # type: ignore[attr-defined]
            await self._done(ctx, "Redémarrage demandé (code de sortie 3)…")
            await self.bot.close()


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(Owner(bot))
