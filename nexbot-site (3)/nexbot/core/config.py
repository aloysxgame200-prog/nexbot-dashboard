"""Configuration centralisée de Nexbot.

Toutes les valeurs sensibles proviennent des variables d'environnement
(fichier `.env` en local, variables du projet sur Vercel / l'hébergeur).
Aucun token n'est écrit dans le code source.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

try:  # python-dotenv est optionnel en production
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BOT_NAME = "Nexbot"
VERSION = "0.1.0"
DEFAULT_SUPPORT_URL = "https://discord.gg/BFDZ7577rZ"

# Permissions demandées à l'invitation du bot (voir README).
# Administrator n'est volontairement PAS demandé : seulement ce qui est nécessaire.
BOT_INVITE_PERMISSIONS = (
    0x2          # KICK_MEMBERS
    | 0x4        # BAN_MEMBERS
    | 0x10       # MANAGE_CHANNELS
    | 0x20       # MANAGE_GUILD
    | 0x80       # VIEW_AUDIT_LOG
    | 0x400      # VIEW_CHANNEL
    | 0x800      # SEND_MESSAGES
    | 0x2000     # MANAGE_MESSAGES
    | 0x4000     # EMBED_LINKS
    | 0x8000     # ATTACH_FILES
    | 0x10000    # READ_MESSAGE_HISTORY
    | 0x40000    # USE_EXTERNAL_EMOJIS
    | 0x40       # ADD_REACTIONS
    | 0x10000000  # MANAGE_ROLES
    | 0x20000000  # MANAGE_WEBHOOKS
    | 0x10000000000  # MODERATE_MEMBERS (timeout)
)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_database_url(url: str) -> str:
    """Accepte les URL Postgres fournies par Neon/Supabase/Railway et les adapte à psycopg 3."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    secret_key: str
    discord_client_id: str
    discord_client_secret: str
    discord_bot_token: str
    dashboard_url: str
    support_url: str
    premium_dev_mode: bool
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_id: str

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.dashboard_url.rstrip('/')}/callback"

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret and self.stripe_price_id)

    def bot_invite_url(self, guild_id: int | None = None) -> str:
        url = (
            "https://discord.com/oauth2/authorize"
            f"?client_id={self.discord_client_id}"
            f"&permissions={BOT_INVITE_PERMISSIONS}"
            "&scope=bot%20applications.commands"
        )
        if guild_id:
            url += f"&guild_id={guild_id}&disable_guild_select=true"
        return url


@lru_cache
def get_settings() -> Settings:
    env = os.getenv("NEXBOT_ENV", "development").strip().lower()
    secret = os.getenv("SECRET_KEY", "")
    if not secret:
        if env == "production":
            raise RuntimeError("SECRET_KEY est obligatoire en production.")
        secret = "dev-only-insecure-secret-change-me"
    dashboard_url = os.getenv("DASHBOARD_URL", "")
    if not dashboard_url and os.getenv("VERCEL_PROJECT_PRODUCTION_URL"):
        dashboard_url = "https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"]
    return Settings(
        env=env,
        database_url=normalize_database_url(os.getenv("DATABASE_URL", "sqlite:///./nexbot.db")),
        secret_key=secret,
        discord_client_id=os.getenv("DISCORD_CLIENT_ID", ""),
        discord_client_secret=os.getenv("DISCORD_CLIENT_SECRET", ""),
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        dashboard_url=dashboard_url or "http://localhost:8000",
        support_url=os.getenv("SUPPORT_URL", DEFAULT_SUPPORT_URL),
        # Le mode Premium de développement est TOUJOURS coupé en production.
        premium_dev_mode=_bool("PREMIUM_DEV_MODE", env != "production") and env != "production",
        stripe_secret_key=os.getenv("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", ""),
        stripe_price_id=os.getenv("STRIPE_PRICE_ID", ""),
    )
