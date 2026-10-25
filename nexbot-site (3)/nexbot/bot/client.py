"""Client Discord de Nexbot : configuration, cache des paramètres, sécurité centrale."""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import Counter, deque
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from ..core import premium as premium_mod
from ..core import settings_store
from ..core.config import BOT_NAME, VERSION, get_settings
from ..core.db import session_scope
from ..core.migrations import run_migrations
from ..core.models import GlobalBlacklist, GlobalState, utcnow
from ..core.security import is_owner
from . import utils

log = logging.getLogger("nexbot.bot")

OWNER_PREFIX = "*"
SETTINGS_TTL = 60.0   # secondes ; invalidé plus tôt dès qu'un changement est détecté en base
PREMIUM_TTL = 60.0

EXTENSIONS = [
    "nexbot.bot.cogs.core",
    "nexbot.bot.cogs.owner",
    "nexbot.bot.cogs.general",
    "nexbot.bot.cogs.logs",
    "nexbot.bot.cogs.moderation",
    "nexbot.bot.cogs.automod",
    "nexbot.bot.cogs.antiraid",
    "nexbot.bot.cogs.welcome",
    "nexbot.bot.cogs.tickets",
    "nexbot.bot.cogs.community",
    "nexbot.bot.cogs.automations",
]

# Modules qu'un propriétaire peut couper globalement avec *module (jamais l'anti-raid par défaut)
GLOBAL_MODULES = ["moderation", "automod", "antiraid", "logs", "welcome", "autoroles", "tickets",
                  "suggestions", "reports", "automations", "scheduled"]

DEFAULT_PRESENCE = {"status": "online", "type": "watching", "text": "/help · nexbot", "url": ""}

JobHandler = Callable[[Any], Awaitable[tuple[bool, str]]]


class NexTree(app_commands.CommandTree):
    """Contrôles appliqués à TOUTES les commandes slash et interactions de commandes."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: Nexbot = interaction.client  # type: ignore[assignment]
        if is_owner(interaction.user.id):
            return True
        if interaction.user.id in bot.blacklist_users or (interaction.guild_id and interaction.guild_id in bot.blacklist_guilds):
            await utils.reply(interaction, utils.err("Vous ne pouvez pas utiliser Nexbot."))
            return False
        if bot.maintenance.get("enabled"):
            msg = bot.maintenance.get("message") or "Nexbot est en maintenance, réessayez dans quelques minutes."
            await utils.reply(interaction, utils.warn_embed(f"🛠️ {msg}"))
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            text = "Il vous manque la permission : " + ", ".join(p.replace("_", " ") for p in error.missing_permissions)
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "Il me manque la permission : " + ", ".join(p.replace("_", " ") for p in error.missing_permissions)
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Patientez {error.retry_after:.0f} s avant de réutiliser cette commande."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "Cette commande s'utilise uniquement sur un serveur."
        elif isinstance(error, app_commands.CheckFailure):
            text = str(error) or "Vous n'avez pas la permission d'utiliser cette commande."
        elif isinstance(original, discord.Forbidden):
            text = "Discord m'a refusé cette action : vérifiez mes permissions et la position de mon rôle."
        elif isinstance(original, discord.NotFound):
            text = "Élément introuvable (supprimé entre-temps ?)."
        else:
            text = "Une erreur inattendue est survenue. Elle a été enregistrée."
            bot: Nexbot = interaction.client  # type: ignore[assignment]
            bot.capture_error(f"/{interaction.command.qualified_name if interaction.command else '?'}", original,
                              interaction.guild)
        try:
            await utils.reply(interaction, utils.err(text))
        except discord.HTTPException:
            pass


class Nexbot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True           # privilégié : arrivées, rôles, anti-raid
        intents.message_content = True   # privilégié : filtres, anti-spam, logs de messages
        intents.moderation = True        # bans + journal d'audit en temps réel (anti-nuke)
        intents.webhooks = True
        intents.presences = False
        super().__init__(
            command_prefix=OWNER_PREFIX, intents=intents, help_command=None, case_insensitive=True,
            tree_cls=NexTree, max_messages=5000,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
            activity=discord.Activity(type=discord.ActivityType.watching, name="démarrage…"),
        )
        self.started_at = utcnow()
        self.settings = get_settings()
        self._settings_cache: dict[tuple[int, str], tuple[float, Any]] = {}
        self._premium_cache: dict[int, tuple[float, bool]] = {}
        self.maintenance: dict[str, Any] = {"enabled": False, "message": ""}
        self.presence_conf: dict[str, Any] = dict(DEFAULT_PRESENCE)
        self.disabled_modules: set[str] = set()
        self.blacklist_users: set[int] = set()
        self.blacklist_guilds: set[int] = set()
        # Tampons d'utilisation, vidés en base toutes les minutes
        self.usage_users: set[int] = set()
        self.usage_commands: Counter[str] = Counter()
        self.stat_buffer: Counter[tuple[int, str]] = Counter()
        self.job_handlers: dict[str, JobHandler] = {}
        self.recent_errors: deque[dict] = deque(maxlen=50)
        # Vérification centrale : toute commande à préfixe (*) est réservée au propriétaire.
        self.add_check(lambda ctx: is_owner(ctx.author.id))

    # ------------------------------------------------------------------ démarrage
    async def setup_hook(self) -> None:
        await self.db(run_migrations)
        await self.load_global_state()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        log.info("%s v%s : %d modules chargés", BOT_NAME, VERSION, len(self.extensions))

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (%s) sur %d serveurs", self.user, self.user.id if self.user else "?",
                 len(self.guilds))
        await self.apply_presence()

    # ------------------------------------------------------------------ sécurité des commandes *
    async def on_message(self, message: discord.Message) -> None:
        """Les commandes `*` ne sont traitées QUE pour le propriétaire.

        Pour tout autre auteur : aucun traitement, aucune réponse, aucun effet de bord.
        Les autres modules (filtres, logs…) écoutent on_message de leur côté.
        """
        if message.author.bot or not message.content.startswith(OWNER_PREFIX):
            return
        if not is_owner(message.author.id):
            return
        await self.process_commands(message)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if not is_owner(ctx.author.id):
            return  # silence absolu (ne devrait jamais arriver : filtrage en amont)
        if isinstance(error, commands.CommandNotFound):
            return
        original = getattr(error, "original", error)
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.TooManyArguments)):
            usage = f"`*{ctx.command.qualified_name} {ctx.command.signature}`" if ctx.command else ""
            await ctx.reply(embed=utils.err(f"{error}\nUtilisation : {usage}"), mention_author=False)
            return
        self.capture_error(f"*{ctx.command.qualified_name if ctx.command else '?'}", original, ctx.guild)
        await ctx.reply(embed=utils.err(f"Erreur : `{type(original).__name__}: {utils.truncate(str(original), 300)}`"),
                        mention_author=False)

    # ------------------------------------------------------------------ base de données
    async def db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Exécute une fonction de base de données sans bloquer la boucle Discord."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def load_global_state(self) -> None:
        def load():
            with session_scope() as db:
                states = {s.key: dict(s.value or {}) for s in db.query(GlobalState).all()}
                bl = [(b.target_id, b.kind) for b in db.query(GlobalBlacklist).all()]
            return states, bl

        states, bl = await self.db(load)
        self.maintenance = {"enabled": False, "message": "", **states.get("maintenance", {})}
        self.presence_conf = {**DEFAULT_PRESENCE, **states.get("presence", {})}
        self.disabled_modules = set(states.get("modules", {}).get("disabled", []))
        self.blacklist_users = {t for t, k in bl if k == "user"}
        self.blacklist_guilds = {t for t, k in bl if k == "guild"}

    async def save_global_state(self, key: str, value: dict) -> None:
        def save():
            with session_scope() as db:
                row = db.get(GlobalState, key)
                if row is None:
                    db.add(GlobalState(key=key, value=value))
                else:
                    row.value = value
        await self.db(save)

    async def apply_presence(self) -> None:
        conf = self.presence_conf
        status = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd,
                  "invisible": discord.Status.invisible}.get(conf.get("status"), discord.Status.online)
        text = conf.get("text") or ""
        kind = conf.get("type", "watching")
        activity: discord.BaseActivity | None
        if self.maintenance.get("enabled"):
            activity = discord.CustomActivity(name="🛠️ Maintenance en cours")
            status = discord.Status.dnd if status == discord.Status.online else status
        elif not text:
            activity = None
        elif kind == "streaming" and conf.get("url"):
            activity = discord.Streaming(name=text, url=conf["url"])
        elif kind == "custom":
            activity = discord.CustomActivity(name=text)
        else:
            atype = {"playing": discord.ActivityType.playing, "watching": discord.ActivityType.watching,
                     "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing
                     }.get(kind, discord.ActivityType.watching)
            activity = discord.Activity(type=atype, name=text)
        await self.change_presence(status=status, activity=activity)

    # ------------------------------------------------------------------ paramètres des serveurs
    async def is_premium(self, guild_id: int) -> bool:
        now = time.monotonic()
        hit = self._premium_cache.get(guild_id)
        if hit and now - hit[0] < PREMIUM_TTL:
            return hit[1]

        def load():
            with session_scope() as db:
                return premium_mod.is_premium(db, guild_id)
        value = await self.db(load)
        self._premium_cache[guild_id] = (now, value)
        return value

    async def conf(self, guild_id: int, module: str) -> Any:
        """Paramètres EFFECTIFS d'un module (options Premium neutralisées si le serveur n'est pas Premium)."""
        now = time.monotonic()
        key = (guild_id, module)
        hit = self._settings_cache.get(key)
        if hit and now - hit[0] < SETTINGS_TTL:
            return hit[1]
        premium = await self.is_premium(guild_id)

        def load():
            with session_scope() as db:
                return settings_store.effective(db, guild_id, module, premium)
        model = await self.db(load)
        self._settings_cache[key] = (now, model)
        return model

    def invalidate(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            self._settings_cache.clear()
            self._premium_cache.clear()
        else:
            for key in [k for k in self._settings_cache if k[0] == guild_id]:
                self._settings_cache.pop(key, None)
            self._premium_cache.pop(guild_id, None)

    def globally_enabled(self, module: str) -> bool:
        return module not in self.disabled_modules

    async def embed_color(self, guild_id: int | None) -> int:
        if not guild_id:
            return utils.DEFAULT_COLOR
        general = await self.conf(guild_id, "general")
        return utils.hex_to_int(general.embed_color)

    async def is_moderator(self, member: discord.Member, permission: str) -> bool:
        """Permission Discord demandée OU rôle modérateur configuré dans le Dashboard."""
        perms = member.guild_permissions
        if perms.administrator or getattr(perms, permission, False):
            return True
        general = await self.conf(member.guild.id, "general")
        role_ids = {int(r) for r in general.mod_role_ids}
        return any(r.id in role_ids for r in member.roles)

    # ------------------------------------------------------------------ statistiques & usage
    def record_usage(self, user_id: int, command: str | None = None) -> None:
        self.usage_users.add(user_id)
        if command:
            self.usage_commands[command[:64]] += 1

    def stat(self, guild_id: int | None, metric: str, n: int = 1) -> None:
        if guild_id:
            self.stat_buffer[(guild_id, metric)] += n

    async def on_app_command_completion(self, interaction: discord.Interaction,
                                        command: app_commands.Command | app_commands.ContextMenu) -> None:
        self.record_usage(interaction.user.id, command.qualified_name)

    # ------------------------------------------------------------------ journal & erreurs
    async def send_log(self, guild: discord.Guild, category: str, embed: discord.Embed,
                       file: discord.File | None = None) -> None:
        cog = self.get_cog("Logs")
        if cog is not None:
            await cog.send(guild, category, embed, file=file)  # type: ignore[attr-defined]

    def capture_error(self, where: str, error: BaseException, guild: discord.Guild | None = None) -> None:
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        log.error("Erreur dans %s : %s", where, tb)
        self.recent_errors.append({"where": where, "error": f"{type(error).__name__}: {error}"[:500],
                                   "guild": guild.id if guild else None, "at": utcnow()})
        if guild is not None:
            embed = discord.Embed(title="Erreur de Nexbot", color=utils.DANGER,
                                  description=f"**{where}**\n`{utils.truncate(type(error).__name__ + ': ' + str(error), 1000)}`")
            asyncio.create_task(self.send_log(guild, "errors", embed))

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        import sys

        error = sys.exc_info()[1]
        if error is not None:
            guild = next((getattr(a, "guild", None) for a in args if getattr(a, "guild", None)), None)
            self.capture_error(f"événement {event_method}", error, guild)

    # ------------------------------------------------------------------ jobs envoyés par le Dashboard
    def register_job(self, kind: str, handler: JobHandler) -> None:
        self.job_handlers[kind] = handler
