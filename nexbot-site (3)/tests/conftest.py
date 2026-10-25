"""Environnement de test : base SQLite temporaire et API Discord simulée (aucun appel réseau)."""
from __future__ import annotations

import os
import re
import tempfile
from urllib.parse import parse_qs, urlparse

_tmp = tempfile.mkdtemp(prefix="nexbot-tests-")
os.environ.update({
    "NEXBOT_ENV": "development",
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "SECRET_KEY": "test-secret",
    "DISCORD_CLIENT_ID": "111111111111111111",
    "DISCORD_CLIENT_SECRET": "client-secret",
    "DISCORD_BOT_TOKEN": "bot-token",
    "DASHBOARD_URL": "http://testserver",
    "PREMIUM_DEV_MODE": "true",
    "STRIPE_SECRET_KEY": "",
    "STRIPE_WEBHOOK_SECRET": "",
    "STRIPE_PRICE_ID": "",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from nexbot.core.security import OWNER_ID  # noqa: E402

GUILD_A = 200000000000000001   # Nexbot présent ; alice admin ; bob simple membre ; propriétaire admin
GUILD_B = 200000000000000002   # Nexbot présent ; bob « Gérer le serveur »
GUILD_C = 200000000000000003   # Nexbot absent ; alice propriétaire

USERS = {
    "alice": {"id": "300000000000000001", "username": "alice", "global_name": "Alice", "avatar": None},
    "bob": {"id": "300000000000000002", "username": "bob", "global_name": "Bob", "avatar": None},
    "owner": {"id": str(OWNER_ID), "username": "owner", "global_name": "Owner", "avatar": None},
}
GUILDS = {
    "alice": [{"id": str(GUILD_A), "name": "Serveur A", "icon": None, "owner": False, "permissions": "8"},
              {"id": str(GUILD_C), "name": "Serveur C", "icon": None, "owner": True, "permissions": "0"}],
    "bob": [{"id": str(GUILD_A), "name": "Serveur A", "icon": None, "owner": False, "permissions": "1024"},
            {"id": str(GUILD_B), "name": "Serveur B", "icon": None, "owner": False, "permissions": "32"}],
    "owner": [{"id": str(GUILD_A), "name": "Serveur A", "icon": None, "owner": False, "permissions": "8"}],
}
BOT_GUILDS = {GUILD_A, GUILD_B}
CHANNEL_A = "400000000000000001"
ROLE_A = "500000000000000001"


def discord_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api/v10")
    auth = request.headers.get("authorization", "")
    if path == "/oauth2/token":
        form = parse_qs(request.content.decode())
        code = form.get("code", form.get("refresh_token", [""]))[0]
        if code not in USERS:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": f"tok-{code}", "refresh_token": code, "expires_in": 604800})
    if auth.startswith("Bearer tok-"):
        who = auth.removeprefix("Bearer tok-")
        if path == "/users/@me":
            return httpx.Response(200, json=USERS[who])
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=GUILDS[who])
    if auth == "Bot bot-token":
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": str(g), "name": f"Serveur {g}", "icon": None}
                                             for g in sorted(BOT_GUILDS)])
        m = re.fullmatch(r"/guilds/(\d+)(/channels|/roles)?", path)
        if m:
            gid = int(m.group(1))
            if gid not in BOT_GUILDS:
                return httpx.Response(404, json={"message": "Unknown Guild"})
            if m.group(2) == "/channels":
                return httpx.Response(200, json=[{"id": CHANNEL_A, "name": "général", "type": 0, "position": 0}])
            if m.group(2) == "/roles":
                return httpx.Response(200, json=[{"id": str(gid), "name": "@everyone", "position": 0},
                                                 {"id": ROLE_A, "name": "Membre", "position": 1, "color": 0}])
            return httpx.Response(200, json={"id": str(gid), "name": "x"})
    return httpx.Response(401, json={"message": "401: Unauthorized"})


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    from nexbot.core.db import session_scope
    from nexbot.core.migrations import run_migrations
    from nexbot.core.models import Guild

    run_migrations()
    with session_scope() as db:
        for gid in (GUILD_A, GUILD_B):
            if db.get(Guild, gid) is None:
                db.add(Guild(id=gid, name=f"Serveur {gid}", bot_present=True))
    yield


@pytest.fixture(autouse=True)
def _mock_discord():
    from nexbot.dashboard import discord_api, ratelimit

    discord_api.transport = httpx.MockTransport(discord_handler)
    discord_api.clear_cache()
    ratelimit.reset()
    yield
    discord_api.transport = None


@pytest.fixture
def make_client():
    from fastapi.testclient import TestClient

    from nexbot.dashboard.app import create_app

    app = create_app()

    def _make(who: str | None = None) -> TestClient:
        client = TestClient(app)
        if who:
            r = client.get("/login", follow_redirects=False)
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            r = client.get(f"/callback?code={who}&state={state}", follow_redirects=False)
            assert r.status_code == 302, r.text
            # Jeton CSRF de la session : lu comme le ferait la page
            page = client.get(f"/g/{GUILD_A}") if who != "bob" else client.get(f"/g/{GUILD_B}")
            m = re.search(r'"csrf": "([^"]+)"', page.text)
            client.headers["X-CSRF-Token"] = m.group(1)
        return client

    return _make


@pytest.fixture
def clean_guild_data():
    """Remet à zéro les données des serveurs de test entre deux tests."""
    from sqlalchemy import delete

    from nexbot.core.db import session_scope
    from nexbot.core import models

    def _clean():
        with session_scope() as db:
            for t in (models.GuildSettings, models.Subscription, models.Automation, models.BotJob, models.Sanction,
                      models.ScheduledMessage, models.StripeEvent, models.ModNote, models.DailyStat,
                      models.ActiveUser, models.CommandUsage, models.GlobalBlacklist, models.SecurityIncident):
                db.execute(delete(t))
    _clean()
    yield
    _clean()
