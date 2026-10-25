"""Diagnostic de configuration : au lieu de planter, le Dashboard explique ce qui manque."""
from __future__ import annotations

import html
import os
import re

from ..core.config import get_settings


def _mask(text: str) -> str:
    # Ne jamais afficher un mot de passe contenu dans une URL de base de données
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1****@", text)


def config_problems() -> list[str]:
    problems: list[str] = []
    try:
        s = get_settings()
    except Exception as exc:  # ex. SECRET_KEY manquante en production
        return [f"Configuration invalide : {exc}"]
    on_vercel = bool(os.getenv("VERCEL"))
    if s.database_url.startswith("sqlite") and on_vercel:
        problems.append("DATABASE_URL est absente : Vercel ne peut pas utiliser SQLite. "
                        "Ajoutez l'adresse PostgreSQL de Neon (postgresql://…).")
    if not s.discord_client_id:
        problems.append("DISCORD_CLIENT_ID est absente.")
    if not s.discord_client_secret:
        problems.append("DISCORD_CLIENT_SECRET est absente.")
    if not s.discord_bot_token:
        problems.append("DISCORD_BOT_TOKEN est absente.")
    if on_vercel and not os.getenv("DASHBOARD_URL"):
        problems.append("DASHBOARD_URL est absente (ex. https://votre-projet.vercel.app).")
    if s.discord_client_id and not s.discord_client_id.isdigit():
        problems.append("DISCORD_CLIENT_ID doit être un nombre (Application ID), pas une clé.")
    return problems


def describe_db_error(exc: BaseException) -> str:
    return _mask(f"{type(exc).__name__}: {exc}")[:500]


def render_page(problems: list[str]) -> str:
    items = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Nexbot — configuration</title>
<style>body{{background:#0B0B12;color:#ECECF4;font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}}
main{{max-width:640px;background:#15151F;border:1px solid #2A2A3B;border-radius:14px;padding:28px}}
h1{{margin-top:0;font-size:1.4rem}}li{{margin:8px 0;color:#FBBF24}}p{{color:#9A9AB0}}code{{background:#1C1C29;padding:1px 6px;border-radius:6px}}</style>
</head><body><main><h1>Nexbot : configuration incomplète</h1>
<p>Le site fonctionne, mais il lui manque des réglages :</p><ul>{items}</ul>
<p>Corrigez-les dans Vercel → <code>Settings</code> → <code>Environment Variables</code>, puis
<b>redéployez</b> (onglet <code>Deployments</code> → <code>⋯</code> → <code>Redeploy</code>) : les variables ne
s'appliquent qu'aux nouveaux déploiements.</p></main></body></html>"""
