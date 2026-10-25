"""Client HTTP minimal pour l'API Discord (OAuth2 utilisateur + lecture avec le token du bot)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..core.config import get_settings

API = "https://discord.com/api/v10"
OAUTH_SCOPES = "identify guilds"

# Remplaçable dans les tests (httpx.MockTransport)
transport: httpx.AsyncBaseTransport | None = None

_cache: dict[tuple, tuple[float, Any]] = {}
CACHE_TTL = 30.0


class DiscordError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"Discord API {status}: {message}")
        self.status = status


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=10.0, transport=transport,
                             headers={"User-Agent": "Nexbot Dashboard (https://discord.gg/BFDZ7577rZ, 0.1)"})


async def _request(method: str, path: str, *, auth: str, **kw) -> Any:
    async with _client() as client:
        for attempt in range(3):
            resp = await client.request(method, path, headers={"Authorization": auth}, **kw)
            if resp.status_code == 429 and attempt < 2:  # limite de l'API : on attend comme demandé
                retry = float(resp.json().get("retry_after", 1.0)) if resp.content else 1.0
                await asyncio.sleep(min(retry, 5.0))
                continue
            if resp.status_code >= 400:
                raise DiscordError(resp.status_code, resp.text[:200])
            return resp.json() if resp.content else None
    raise DiscordError(429, "rate limited")


# ------------------------------------------------------------------- OAuth2
def authorize_url(state: str) -> str:
    s = get_settings()
    from urllib.parse import urlencode

    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": s.discord_client_id, "redirect_uri": s.oauth_redirect_uri, "response_type": "code",
        "scope": OAUTH_SCOPES, "state": state, "prompt": "none",
    })


async def _token_request(data: dict) -> dict:
    s = get_settings()
    async with _client() as client:
        resp = await client.post("/oauth2/token", data={**data, "client_id": s.discord_client_id,
                                                         "client_secret": s.discord_client_secret},
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        raise DiscordError(resp.status_code, resp.text[:200])
    return resp.json()


async def exchange_code(code: str) -> dict:
    return await _token_request({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": get_settings().oauth_redirect_uri})


async def refresh_token(token: str) -> dict:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": token})


async def get_user(access_token: str) -> dict:
    return await _request("GET", "/users/@me", auth=f"Bearer {access_token}")


async def get_user_guilds(access_token: str) -> list[dict]:
    return await _request("GET", "/users/@me/guilds", auth=f"Bearer {access_token}")


# ------------------------------------------------------------------- lecture avec le token du bot
def _bot_auth() -> str:
    token = get_settings().discord_bot_token
    if not token:
        raise DiscordError(503, "DISCORD_BOT_TOKEN non configuré")
    return f"Bot {token}"


async def _cached(key: tuple, coro_factory):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = await coro_factory()
    _cache[key] = (now, value)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
            _cache.pop(k, None)
    return value


async def bot_in_guild(guild_id: int) -> bool:
    async def fetch():
        try:
            await _request("GET", f"/guilds/{guild_id}", auth=_bot_auth())
            return True
        except DiscordError as e:
            if e.status in (403, 404):
                return False
            raise
    return await _cached(("present", guild_id), fetch)


async def guild_channels(guild_id: int) -> list[dict]:
    async def fetch():
        raw = await _request("GET", f"/guilds/{guild_id}/channels", auth=_bot_auth())
        out = [{"id": c["id"], "name": c.get("name", ""), "type": c.get("type", 0),
                "position": c.get("position", 0), "parent_id": c.get("parent_id")} for c in raw]
        return sorted(out, key=lambda c: (c["type"] == 4, c["position"]))
    return await _cached(("channels", guild_id), fetch)


async def guild_roles(guild_id: int) -> list[dict]:
    async def fetch():
        raw = await _request("GET", f"/guilds/{guild_id}/roles", auth=_bot_auth())
        out = [{"id": r["id"], "name": r.get("name", ""), "color": r.get("color", 0),
                "position": r.get("position", 0), "managed": r.get("managed", False)}
               for r in raw if r["id"] != str(guild_id)]  # @everyone exclu
        return sorted(out, key=lambda r: -r["position"])
    return await _cached(("roles", guild_id), fetch)


def clear_cache() -> None:
    _cache.clear()
