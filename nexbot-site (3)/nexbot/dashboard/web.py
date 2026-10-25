"""Moteur de templates partagé."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..core.config import BOT_NAME, VERSION, get_settings

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _tojson_safe(value) -> Markup:
    """JSON sûr à insérer dans une balise <script type="application/json">."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(raw)


templates.env.filters["safejson"] = _tojson_safe
templates.env.globals.update(BOT_NAME=BOT_NAME, VERSION=VERSION, settings=get_settings)


def guild_icon(guild_id, icon) -> str | None:
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon}.png?size=128" if icon else None


templates.env.globals["guild_icon"] = guild_icon
