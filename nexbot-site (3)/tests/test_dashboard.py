"""Tests du Dashboard : authentification, permissions, isolation, Premium, CSRF, pages."""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexbot.core import settings_store
from nexbot.core.db import session_scope
from nexbot.core.models import BotJob, Sanction, Subscription, utcnow

from .conftest import CHANNEL_A, GUILD_A, GUILD_B, GUILD_C, ROLE_A

pytestmark = pytest.mark.usefixtures("clean_guild_data")


def grant_premium(guild_id: int, days: int = 30) -> None:
    with session_scope() as db:
        db.add(Subscription(guild_id=guild_id, status="active", source="manual",
                            current_period_end=utcnow() + timedelta(days=days)))


def expire_premium(guild_id: int) -> None:
    with session_scope() as db:
        for s in db.query(Subscription).filter_by(guild_id=guild_id):
            s.status = "expired"


# ----------------------------------------------------------------- pages publiques & OAuth
def test_public_pages_and_security_headers(make_client):
    c = make_client()
    for path in ("/", "/help", "/premium", "/healthz"):
        r = c.get(path)
        assert r.status_code == 200, path
    r = c.get("/")
    assert "Nexbot" in r.text and "Sentrix" not in r.text
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert c.get("/support", follow_redirects=False).headers["location"] == "https://discord.gg/BFDZ7577rZ"


def test_oauth_rejects_bad_state(make_client):
    c = make_client()
    c.get("/login", follow_redirects=False)
    r = c.get("/callback?code=alice&state=forged", follow_redirects=False)
    assert r.status_code == 400


def test_servers_page_lists_manageable_guilds(make_client):
    c = make_client("alice")
    r = c.get("/servers")
    assert r.status_code == 200
    assert "Serveur A" in r.text and "Serveur C" in r.text      # A : admin, C : propriétaire
    assert "Configurer" in r.text and "Inviter Nexbot" in r.text  # C : Nexbot absent


def test_bot_presence_comes_from_discord_not_only_db(make_client):
    """Le bot n'a encore jamais écrit en base : Discord fait foi, et la base est mise à jour."""
    from nexbot.core.models import Guild
    from . import conftest

    with session_scope() as db:
        db.query(Guild).filter_by(id=GUILD_C).delete()
    conftest.BOT_GUILDS.add(GUILD_C)
    try:
        alice = make_client("alice")
        page = alice.get("/servers?refresh=1").text
        assert page.count("Configurer") == 2 and "Inviter Nexbot" not in page
        assert alice.get(f"/api/guilds/{GUILD_C}/modules/moderation").status_code == 200
        with session_scope() as db:
            assert db.get(Guild, GUILD_C).bot_present is True
    finally:
        conftest.BOT_GUILDS.discard(GUILD_C)


def test_unauthenticated_api_is_rejected(make_client):
    c = make_client()
    assert c.get(f"/api/guilds/{GUILD_A}/modules/moderation").status_code == 401
    assert c.get(f"/g/{GUILD_A}", follow_redirects=False).headers["location"] == "/login"


# ----------------------------------------------------------------- permissions & isolation
def test_user_without_manage_guild_is_refused(make_client):
    bob = make_client("bob")  # simple membre de A
    assert bob.get(f"/api/guilds/{GUILD_A}/modules/moderation").status_code == 403
    assert bob.put(f"/api/guilds/{GUILD_A}/modules/moderation", json={"enabled": False}).status_code == 403
    assert bob.get(f"/api/guilds/{GUILD_B}/modules/moderation").status_code == 200


def test_guild_without_bot_returns_404(make_client):
    alice = make_client("alice")
    assert alice.get(f"/api/guilds/{GUILD_C}/modules/moderation").status_code == 404


def test_csrf_is_required(make_client):
    alice = make_client("alice")
    token = alice.headers.pop("X-CSRF-Token")
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/moderation", json={"enabled": False}).status_code == 403
    alice.headers["X-CSRF-Token"] = "wrong"
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/moderation", json={"enabled": False}).status_code == 403
    alice.headers["X-CSRF-Token"] = token
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/moderation", json={"enabled": False},
                  headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_settings_saved_and_isolated_between_guilds(make_client):
    alice, bob = make_client("alice"), make_client("bob")
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/moderation",
                  json={"banned_words": ["spam", "arnaque"], "warns_to_timeout": 2, "exempt_role_ids": [ROLE_A]})
    assert r.status_code == 200, r.text
    got = alice.get(f"/api/guilds/{GUILD_A}/modules/moderation").json()["values"]
    assert got["banned_words"] == ["spam", "arnaque"] and got["warns_to_timeout"] == 2
    assert got["exempt_role_ids"] == [ROLE_A]
    other = bob.get(f"/api/guilds/{GUILD_B}/modules/moderation").json()["values"]
    assert other["banned_words"] == [] and other["warns_to_timeout"] == 3


def test_invalid_values_are_rejected_with_field_errors(make_client):
    alice = make_client("alice")
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/moderation", json={"spam_max_messages": 999})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "spam_max_messages"
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/welcome", json={"text": "Salut {moderator}"})
    assert r.status_code == 422 and "{moderator}" in r.json()["message"]
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/antiraid", json={"alert_channel_id": "pas-un-id"})
    assert r.status_code == 422
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/antiraid", json={"nuke_action": "ban"})
    assert r.status_code == 422 and "confirmation" in r.json()["message"]
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/antiraid", json={"nuke_action": "ban", "confirm_destructive": True})
    assert r.status_code == 200


# ----------------------------------------------------------------- Premium
def test_premium_options_locked_on_free_plan(make_client):
    alice = make_client("alice")
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/moderation", json={"msg_ban": "Au revoir {user}"})
    assert r.status_code == 402
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/theme", json={"primary": "#FF0000"}).status_code == 402
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/autoroles", json={"role_ids": [ROLE_A, "500000000000000002",
                                                                                 "500000000000000003"]})
    assert r.status_code == 402 and "limite gratuite" in r.json()["message"]
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/autoroles",
                     json={"role_ids": [ROLE_A, "500000000000000002"]}).status_code == 200


def test_premium_expiry_keeps_settings_and_resubscribe_restores(make_client):
    alice = make_client("alice")
    grant_premium(GUILD_A)
    embed = {"title": "Bienvenue", "description": "Salut {user.mention}", "color": "#FF00AA", "fields": []}
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/theme", json={"primary": "#FF0000"}).status_code == 200
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/welcome", json={"enabled": True, "embed": embed}).status_code == 200

    expire_premium(GUILD_A)
    with session_scope() as db:
        assert settings_store.stored(db, GUILD_A, "theme").primary == "#FF0000"           # conservé
        assert settings_store.effective(db, GUILD_A, "theme", False).primary == "#7C5CFF"   # non appliqué
        assert settings_store.effective(db, GUILD_A, "welcome", False).embed is None
        assert settings_store.effective(db, GUILD_A, "welcome", False).enabled is True       # le gratuit reste actif
    # Sans Premium, on peut toujours modifier les options gratuites sans perdre les options Premium
    assert alice.put(f"/api/guilds/{GUILD_A}/modules/welcome", json={"enabled": False}).status_code == 200
    page = alice.get(f"/api/guilds/{GUILD_A}/premium").json()
    assert page["premium"] is False and page["subscription"]["status"] == "expired"

    grant_premium(GUILD_A)  # réabonnement
    with session_scope() as db:
        assert settings_store.effective(db, GUILD_A, "theme", True).primary == "#FF0000"
        assert settings_store.effective(db, GUILD_A, "welcome", True).embed.title == "Bienvenue"


def test_premium_cannot_be_faked_by_the_client(make_client):
    alice = make_client("alice")
    r = alice.put(f"/api/guilds/{GUILD_A}/modules/theme", json={"primary": "#FF0000"},
                  headers={"X-Premium": "true"}, params={"premium": "1"})
    assert r.status_code == 402
    assert alice.post(f"/api/guilds/{GUILD_A}/premium/dev", json={"action": "grant"}).status_code == 404


def test_dev_premium_is_owner_only(make_client):
    owner = make_client("owner")
    assert owner.get(f"/api/guilds/{GUILD_A}/premium").json()["dev_premium"] is True
    assert owner.post(f"/api/guilds/{GUILD_A}/premium/dev", json={"action": "grant", "days": 7}).status_code == 200
    assert owner.get(f"/api/guilds/{GUILD_A}/premium").json()["premium"] is True
    assert owner.post(f"/api/guilds/{GUILD_A}/premium/dev", json={"action": "revoke"}).status_code == 200
    assert owner.get(f"/api/guilds/{GUILD_A}/premium").json()["premium"] is False
    alice = make_client("alice")
    assert alice.get(f"/api/guilds/{GUILD_A}/premium").json()["dev_premium"] is False


def test_checkout_without_stripe_is_cleanly_disabled(make_client):
    alice = make_client("alice")
    r = alice.post(f"/api/guilds/{GUILD_A}/premium/checkout")
    assert r.status_code == 503
    assert make_client().post("/api/stripe/webhook", content=b"{}").status_code == 503


# ----------------------------------------------------------------- automatisations
def test_automation_crud_limits_and_validation(make_client):
    alice = make_client("alice")
    good = {"name": "Accueil", "definition": {
        "trigger": {"type": "member_join"},
        "conditions": [{"type": "account_age_below", "days": 3}],
        "actions": [{"type": "add_role", "role_id": ROLE_A},
                    {"type": "send_message", "channel_id": CHANNEL_A, "text": "Bienvenue {user.mention}"}]}}
    r = alice.post(f"/api/guilds/{GUILD_A}/automations", json=good)
    assert r.status_code == 200, r.text
    auto_id = r.json()["item"]["id"]

    three = dict(good, definition=dict(good["definition"], actions=good["definition"]["actions"] + [
        {"type": "dm_member", "text": "Hey"}]))
    assert alice.post(f"/api/guilds/{GUILD_A}/automations", json=three).status_code == 422  # 2 actions en gratuit

    bad = {"name": "Incohérente", "definition": {"trigger": {"type": "member_leave"},
                                                  "actions": [{"type": "timeout", "minutes": 10}]}}
    assert alice.post(f"/api/guilds/{GUILD_A}/automations", json=bad).status_code == 422
    loop = dict(good, definition=dict(good["definition"], cooldown_seconds=0))
    assert alice.post(f"/api/guilds/{GUILD_A}/automations", json=loop).status_code == 422

    assert alice.post(f"/api/guilds/{GUILD_A}/automations", json=good).status_code == 200
    assert alice.post(f"/api/guilds/{GUILD_A}/automations", json=good).status_code == 200
    assert alice.post(f"/api/guilds/{GUILD_A}/automations", json=good).status_code == 402  # 3 maximum

    r = alice.post(f"/api/guilds/{GUILD_A}/automations/{auto_id}/test")
    assert r.status_code == 200
    with session_scope() as db:
        job = db.get(BotJob, r.json()["job_id"])
        assert job.kind == "test_automation" and job.guild_id == GUILD_A

    bob = make_client("bob")
    assert bob.delete(f"/api/guilds/{GUILD_B}/automations/{auto_id}").status_code == 404  # autre serveur
    assert alice.delete(f"/api/guilds/{GUILD_A}/automations/{auto_id}").status_code == 200


# ----------------------------------------------------------------- éditeur de messages & jobs
def test_send_message_requires_premium_and_creates_job(make_client):
    alice = make_client("alice")
    msg = {"content": "Annonce", "embed": {"title": "Nouveautés sur {server}", "description": "…", "fields": []}}
    payload = {"channel_id": CHANNEL_A, "message": msg}
    assert alice.post(f"/api/guilds/{GUILD_A}/messages/send", json=payload).status_code == 402
    grant_premium(GUILD_A)
    r = alice.post(f"/api/guilds/{GUILD_A}/messages/send", json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert alice.get(f"/api/guilds/{GUILD_A}/jobs/{job_id}").json()["status"] == "pending"
    bob = make_client("bob")
    assert bob.get(f"/api/guilds/{GUILD_B}/jobs/{job_id}").status_code == 404  # isolation
    bad = {"channel_id": CHANNEL_A, "message": {"content": "", "embed": {"title": "x" * 300}}}
    assert alice.post(f"/api/guilds/{GUILD_A}/messages/send", json=bad).status_code == 422


def test_scheduled_messages_minimum_interval(make_client):
    alice = make_client("alice")
    grant_premium(GUILD_A)
    base = {"name": "Rappel", "channel_id": CHANNEL_A, "message": {"content": "Pensez au règlement"}}
    assert alice.post(f"/api/guilds/{GUILD_A}/scheduled", json={**base, "interval_minutes": 5}).status_code == 422
    assert alice.post(f"/api/guilds/{GUILD_A}/scheduled", json={**base, "interval_minutes": 60}).status_code == 200
    assert len(alice.get(f"/api/guilds/{GUILD_A}/scheduled").json()["items"]) == 1


# ----------------------------------------------------------------- sanctions
def test_sanctions_history_isolation_and_revoke(make_client):
    with session_scope() as db:
        db.add_all([
            Sanction(guild_id=GUILD_A, user_id=300000000000000009, moderator_id=1, type="warn", reason="spam"),
            Sanction(guild_id=GUILD_A, user_id=300000000000000009, moderator_id=1, type="ban", reason="raid"),
            Sanction(guild_id=GUILD_B, user_id=300000000000000009, moderator_id=1, type="warn", reason="b"),
            Sanction(guild_id=GUILD_A, user_id=300000000000000008, moderator_id=1, type="warn", reason="vieux",
                     created_at=utcnow() - timedelta(days=60)),
        ])
    alice = make_client("alice")
    items = alice.get(f"/api/guilds/{GUILD_A}/sanctions").json()["items"]
    assert {i["reason"] for i in items} == {"spam", "raid"}  # B exclu, > 30 jours masqué en gratuit
    warn = next(i for i in items if i["type"] == "warn")
    ban = next(i for i in items if i["type"] == "ban")
    assert alice.post(f"/api/guilds/{GUILD_A}/sanctions/{ban['id']}/revoke").status_code == 422
    assert alice.post(f"/api/guilds/{GUILD_A}/sanctions/{warn['id']}/revoke").status_code == 200
    grant_premium(GUILD_A)
    assert len(alice.get(f"/api/guilds/{GUILD_A}/sanctions").json()["items"]) == 3  # historique étendu


def test_guild_page_and_overview_render(make_client):
    alice = make_client("alice")
    r = alice.get(f"/g/{GUILD_A}/antiraid")
    assert r.status_code == 200 and '"section": "antiraid"' in r.text
    assert alice.get(f"/g/{GUILD_A}/nimporte").status_code == 404
    d = alice.get(f"/api/guilds/{GUILD_A}/overview?days=90").json()
    assert d["days"] == 7  # limité à 7 jours en gratuit
    assert d["bot"]["online"] is False
    data = alice.get(f"/api/guilds/{GUILD_A}/discord").json()
    assert data["channels"][0]["id"] == CHANNEL_A and all(r["name"] != "@everyone" for r in data["roles"])
