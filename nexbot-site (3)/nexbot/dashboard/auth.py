"""Sessions, CSRF et contrôle d'accès aux serveurs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from sqlalchemy import delete, select

from ..core import premium as premium_mod
from ..core.config import get_settings
from ..core.db import session_scope
from ..core.models import Guild, WebSession, utcnow
from ..core.security import can_manage_guild, constant_time_equals, decrypt, encrypt, hash_token, new_token
from . import discord_api

COOKIE = "nexbot_session"
STATE_COOKIE = "nexbot_oauth_state"
SESSION_DAYS = 7
GUILDS_CACHE_SECONDS = 60


def _aware(dt):
    from datetime import timezone

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cookie_kwargs() -> dict:
    return {"httponly": True, "secure": get_settings().is_production, "samesite": "lax", "path": "/"}


def create_session(user: dict, token: dict) -> str:
    s = get_settings()
    raw = new_token()
    now = utcnow()
    with session_scope() as db:
        db.execute(delete(WebSession).where(WebSession.expires_at < now))  # ménage
        db.add(WebSession(
            id=hash_token(raw), user_id=int(user["id"]),
            username=user.get("global_name") or user.get("username", ""), avatar=user.get("avatar"),
            access_token_enc=encrypt(s.secret_key, token["access_token"]),
            refresh_token_enc=encrypt(s.secret_key, token.get("refresh_token", "")),
            token_expires_at=now + timedelta(seconds=int(token.get("expires_in", 604800))),
            csrf_token=new_token(24), expires_at=now + timedelta(days=SESSION_DAYS),
        ))
    return raw


def destroy_session(raw: str | None) -> None:
    if raw:
        with session_scope() as db:
            db.execute(delete(WebSession).where(WebSession.id == hash_token(raw)))


@dataclass
class CurrentUser:
    session_id: str
    id: int
    username: str
    avatar: str | None
    csrf_token: str

    @property
    def avatar_url(self) -> str:
        if self.avatar:
            return f"https://cdn.discordapp.com/avatars/{self.id}/{self.avatar}.png?size=64"
        return f"https://cdn.discordapp.com/embed/avatars/{(self.id >> 22) % 6}.png"


def current_user(request: Request) -> CurrentUser | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    with session_scope() as db:
        ws = db.get(WebSession, hash_token(raw))
        if ws is None or _aware(ws.expires_at) < utcnow():
            return None
        return CurrentUser(ws.id, ws.user_id, ws.username, ws.avatar, ws.csrf_token)


def require_user(request: Request) -> CurrentUser:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Connexion requise")
    return user


def check_csrf(request: Request, user: CurrentUser) -> None:
    """Toute requête qui modifie des données doit présenter le jeton CSRF de la session
    et provenir de l'origine du Dashboard."""
    token = request.headers.get("x-csrf-token", "")
    if not token or not constant_time_equals(token, user.csrf_token):
        raise HTTPException(403, "Jeton CSRF invalide, rechargez la page.")
    origin = request.headers.get("origin")
    if origin:
        expected = urlparse(get_settings().dashboard_url)
        got = urlparse(origin)
        same_host = got.netloc == request.url.netloc or got.netloc == expected.netloc
        if not same_host:
            raise HTTPException(403, "Origine refusée")


async def _access_token(user: CurrentUser) -> str:
    s = get_settings()
    with session_scope() as db:
        ws = db.get(WebSession, user.session_id)
        if ws is None:
            raise HTTPException(401, "Session expirée")
        access = decrypt(s.secret_key, ws.access_token_enc)
        refresh = decrypt(s.secret_key, ws.refresh_token_enc) if ws.refresh_token_enc else None
        expired = _aware(ws.token_expires_at) - timedelta(minutes=5) < utcnow()
    if access and not expired:
        return access
    if not refresh:
        raise HTTPException(401, "Session expirée")
    try:
        token = await discord_api.refresh_token(refresh)
    except discord_api.DiscordError:
        raise HTTPException(401, "Session expirée, reconnectez-vous")
    with session_scope() as db:
        ws = db.get(WebSession, user.session_id)
        if ws:
            ws.access_token_enc = encrypt(s.secret_key, token["access_token"])
            ws.refresh_token_enc = encrypt(s.secret_key, token.get("refresh_token", refresh))
            ws.token_expires_at = utcnow() + timedelta(seconds=int(token.get("expires_in", 604800)))
    return token["access_token"]


async def user_guilds(user: CurrentUser, force: bool = False) -> list[dict]:
    """Serveurs de l'utilisateur, avec un court cache pour respecter les limites de Discord."""
    with session_scope() as db:
        ws = db.get(WebSession, user.session_id)
        if ws is None:
            raise HTTPException(401, "Session expirée")
        fresh = ws.guilds_cached_at and (utcnow() - _aware(ws.guilds_cached_at)).total_seconds() < GUILDS_CACHE_SECONDS
        if fresh and not force:
            return list(ws.guilds_cache)
    access = await _access_token(user)
    try:
        raw = await discord_api.get_user_guilds(access)
    except discord_api.DiscordError as e:
        if e.status == 401:
            raise HTTPException(401, "Session expirée, reconnectez-vous")
        raise HTTPException(502, "Discord est indisponible, réessayez.")
    guilds = [{"id": g["id"], "name": g.get("name", ""), "icon": g.get("icon"), "owner": bool(g.get("owner")),
               "permissions": str(g.get("permissions", "0"))} for g in raw]
    with session_scope() as db:
        ws = db.get(WebSession, user.session_id)
        if ws:
            ws.guilds_cache = guilds
            ws.guilds_cached_at = utcnow()
    return guilds


def manageable(guilds: list[dict]) -> list[dict]:
    return [g for g in guilds if can_manage_guild(g["permissions"], g["owner"])]


async def present_guild_ids(ids: list[int]) -> set[int]:
    """Serveurs (parmi `ids`) où Nexbot est présent.

    Source de vérité : l'API Discord (token du bot). La base de données ne sert
    que de secours si Discord est injoignable. Les serveurs trouvés sont
    enregistrés en base pour que le bot et le Dashboard partagent la même liste.
    """
    wanted = set(ids)
    try:
        live = await discord_api.bot_guilds()
    except discord_api.DiscordError:
        with session_scope() as db:
            rows = db.scalars(select(Guild.id).where(Guild.id.in_(wanted), Guild.bot_present.is_(True)))
            return set(rows)
    found = wanted & set(live)
    if found:
        with session_scope() as db:
            for gid in found:
                row = db.get(Guild, gid)
                if row is None:
                    db.add(Guild(id=gid, name=live[gid]["name"][:100], icon=live[gid]["icon"], bot_present=True))
                elif not row.bot_present:
                    row.bot_present, row.left_at = True, None
    return found


@dataclass
class GuildContext:
    id: int
    name: str
    icon: str | None
    premium: bool
    user: CurrentUser


async def require_guild(request: Request, guild_id: int, *, mutate: bool = False) -> GuildContext:
    """Point de contrôle unique : l'utilisateur doit pouvoir administrer CE serveur
    et Nexbot doit y être présent. Aucune donnée d'un autre serveur n'est accessible."""
    user = require_user(request)
    if mutate:
        check_csrf(request, user)
    guilds = manageable(await user_guilds(user))
    match = next((g for g in guilds if g["id"] == str(guild_id)), None)
    if match is None:
        raise HTTPException(403, "Vous n'avez pas la permission « Gérer le serveur » sur ce serveur.")
    if guild_id not in await present_guild_ids([guild_id]):
        raise HTTPException(404, "Nexbot n'est pas présent sur ce serveur. Invitez-le puis actualisez la page.")
    with session_scope() as db:
        is_prem = premium_mod.is_premium(db, guild_id)
    return GuildContext(guild_id, match["name"], match["icon"], is_prem, user)
