"""Lancement du bot : python -m nexbot.bot

Code de sortie 3 = redémarrage demandé par *restart (les scripts run_bot.* relancent le bot).
"""
from __future__ import annotations

import logging
import os
import sys

from ..core.config import BOT_NAME, VERSION, get_settings
from ..core.db import get_engine
from .client import Nexbot

RESTART_EXIT_CODE = 3


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s : %(message)s")
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.discord_bot_token:
        print("DISCORD_BOT_TOKEN est vide : ajoutez-le dans le fichier .env (voir .env.example).", file=sys.stderr)
        return 1
    if settings.database_url.startswith("sqlite"):
        print("⚠️  Base SQLite locale : le Dashboard en ligne (Vercel) ne verra PAS ces données. "
              "Utilisez la même DATABASE_URL (Neon) que le Dashboard.", file=sys.stderr)
    get_engine()
    bot = Nexbot()
    logging.getLogger("nexbot").info("Démarrage de %s v%s", BOT_NAME, VERSION)
    bot.run(settings.discord_bot_token, log_handler=None)
    return RESTART_EXIT_CODE if getattr(bot, "restart_requested", False) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
