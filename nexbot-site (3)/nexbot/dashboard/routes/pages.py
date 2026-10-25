"""Pages HTML et authentification Discord OAuth2."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core import premium as premium_mod
from ...core import settings_store
from ...core.config import get_settings
from ...core.db import session_scope
from ...core.schemas import MODULES
from ...core.security import constant_time_equals, is_owner, new_token
from .. import auth, discord_api, ratelimit
from ..web import templates

router = APIRouter()

SECTIONS = ["overview", *MODULES.keys(), "embeds", "automations", "sanctions", "incidents", "premium", "help"]


def _ctx(request: Request, **extra) -> dict:
    return {"request": request, "user": auth.current_user(request), "s": get_settings(), **extra}


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", _ctx(request, features=premium_mod.FEATURES))


@router.get("/login")
async def login(request: Request):
    s = get_settings()
    if not s.discord_client_id or not s.discord_client_secret:
        raise HTTPException(503, "OAuth2 non configuré : définissez DISCORD_CLIENT_ID et DISCORD_CLIENT_SECRET.")
    ratelimit.hit(f"login:{request.client.host if request.client else '?'}", 20, 60)
    state = new_token(16)
    resp = RedirectResponse(discord_api.authorize_url(state), status_code=302)
    resp.set_cookie(auth.STATE_COOKIE, state, max_age=600, **auth.cookie_kwargs())
    return resp


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    expected = request.cookies.get(auth.STATE_COOKIE, "")
    if error:
        return RedirectResponse("/?error=oauth_refused", status_code=302)
    if not code or not state or not expected or not constant_time_equals(state, expected):
        raise HTTPException(400, "Requête de connexion invalide ou expirée. Réessayez.")
    try:
        token = await discord_api.exchange_code(code)
        user = await discord_api.get_user(token["access_token"])
    except discord_api.DiscordError:
        raise HTTPException(502, "Discord a refusé la connexion. Réessayez.")
    raw = auth.create_session(user, token)
    resp = RedirectResponse("/servers", status_code=302)
    resp.delete_cookie(auth.STATE_COOKIE, path="/")
    resp.set_cookie(auth.COOKIE, raw, max_age=auth.SESSION_DAYS * 86400, **auth.cookie_kwargs())
    return resp


@router.post("/logout")
async def logout(request: Request):
    user = auth.require_user(request)
    auth.check_csrf(request, user)
    auth.destroy_session(request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/servers", response_class=HTMLResponse)
async def servers(request: Request, refresh: bool = False):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    guilds = auth.manageable(await auth.user_guilds(user, force=refresh))
    ids = [int(g["id"]) for g in guilds]
    if refresh:
        discord_api.clear_cache()
    present = await auth.present_guild_ids(ids)
    with session_scope() as db:
        prem = {gid for gid in present if premium_mod.is_premium(db, gid)}
    s = get_settings()
    items = [{**g, "present": int(g["id"]) in present, "premium": int(g["id"]) in prem,
              "invite": s.bot_invite_url(int(g["id"]))} for g in guilds]
    items.sort(key=lambda g: (not g["present"], g["name"].lower()))
    return templates.TemplateResponse(request, "servers.html", _ctx(request, guilds=items))


@router.get("/g/{guild_id}", response_class=HTMLResponse)
@router.get("/g/{guild_id}/{section}", response_class=HTMLResponse)
async def guild_page(request: Request, guild_id: int, section: str = "overview"):
    if auth.current_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    if section not in SECTIONS:
        raise HTTPException(404, "Page introuvable")
    ctx = await auth.require_guild(request, guild_id)
    s = get_settings()
    with session_scope() as db:
        theme = settings_store.effective(db, guild_id, "theme", ctx.premium).model_dump()
    boot = {
        "guild": {"id": str(ctx.id), "name": ctx.name, "icon": ctx.icon, "premium": ctx.premium},
        "section": section, "csrf": ctx.user.csrf_token,
        "user": {"id": str(ctx.user.id), "name": ctx.user.username, "avatar": ctx.user.avatar_url},
        "modules": [{"key": m.key, "title": m.title, "icon": m.icon, "description": m.description,
                     "premium_only": m.premium_only} for m in MODULES.values()],
        "limits": premium_mod.limits(ctx.premium), "support_url": s.support_url,
        "dev_premium": s.premium_dev_mode and is_owner(ctx.user.id),
        "stripe_enabled": s.stripe_enabled, "theme": theme,
    }
    return templates.TemplateResponse(request, "guild.html", _ctx(request, boot=boot, guild=ctx, theme=theme))


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", _ctx(request))


@router.get("/premium", response_class=HTMLResponse)
async def premium_page(request: Request):
    return templates.TemplateResponse(request, "premium.html", _ctx(request, features=premium_mod.FEATURES))


@router.get("/invite")
async def invite():
    s = get_settings()
    if not s.discord_client_id:
        raise HTTPException(503, "DISCORD_CLIENT_ID non configuré")
    return RedirectResponse(s.bot_invite_url(), status_code=302)


@router.get("/support")
async def support():
    return RedirectResponse(get_settings().support_url, status_code=302)
