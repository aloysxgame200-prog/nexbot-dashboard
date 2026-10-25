"""Tests du socle partagé : sécurité, embeds, automatisations, Stripe, migrations."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from pydantic import ValidationError

from nexbot.core import automations, billing, embeds, premium
from nexbot.core.db import get_engine, session_scope
from nexbot.core.migrations import LATEST_VERSION, run_migrations
from nexbot.core.models import Subscription, utcnow
from nexbot.core.security import OWNER_ID, decrypt, encrypt, is_owner

from .conftest import CHANNEL_A, GUILD_A, ROLE_A


# ----------------------------------------------------------------- sécurité
@pytest.mark.parametrize("value,expected", [
    (OWNER_ID, True), (str(OWNER_ID), True), (OWNER_ID + 1, False), ("owner", False),
    (None, False), (True, False), (f" {OWNER_ID}", False), (float(OWNER_ID), False),
])
def test_is_owner_is_strict(value, expected):
    assert is_owner(value) is expected


def test_owner_id_is_not_configurable(monkeypatch):
    monkeypatch.setenv("OWNER_ID", "1")
    import importlib

    import nexbot.core.security as sec
    assert importlib.reload(sec).OWNER_ID == 598080343958093831


def test_token_encryption_roundtrip():
    enc = encrypt("k", "secret-token")
    assert "secret-token" not in enc and decrypt("k", enc) == "secret-token"
    assert decrypt("autre-cle", enc) is None


def test_migrations_are_idempotent():
    assert run_migrations(get_engine()) == LATEST_VERSION
    assert run_migrations(get_engine()) == LATEST_VERSION


# ----------------------------------------------------------------- embeds
def test_embed_limits_and_variables():
    e = embeds.EmbedModel(title="Salut {user}", description="Bienvenue sur {server}", color="#ff00aa",
                          thumbnail_url="{user.avatar}", fields=[{"name": "A", "value": "B", "inline": True}])
    assert e.color == "#FF00AA"
    rendered = embeds.render_embed_dict(e, {"user": "Alice", "server": "Nexus", "user.avatar": "https://cdn/x.png"})
    assert rendered["title"] == "Salut Alice" and rendered["thumbnail"]["url"] == "https://cdn/x.png"
    assert rendered["color"] == 0xFF00AA
    with pytest.raises(ValidationError):
        embeds.EmbedModel(title="x" * 257)
    with pytest.raises(ValidationError):
        embeds.EmbedModel(description="a" * 4000, fields=[{"name": "n" * 200, "value": "v" * 1000}] * 3)  # > 6000
    with pytest.raises(ValidationError):
        embeds.EmbedModel(title="ok", image_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        embeds.EmbedModel()  # vide
    with pytest.raises(embeds.TemplateError):
        embeds.MessageModel(content="{reason}").check("welcome")
    embeds.MessageModel(content="{reason}").check("moderation")


def test_render_drops_invalid_urls_after_substitution():
    e = embeds.EmbedModel(title="t", image_url="{user.avatar}")
    out = embeds.render_embed_dict(e, {"user.avatar": "pas une url"})
    assert "image" not in out


# ----------------------------------------------------------------- automatisations
def test_automation_validation_rules():
    ok = {"name": "x", "definition": {"trigger": {"type": "message_contains", "keywords": ["Promo "]},
                                      "conditions": [{"type": "in_channel", "channel_id": CHANNEL_A}],
                                      "actions": [{"type": "delete_message"},
                                                  {"type": "timeout", "minutes": 5}]}}
    a = automations.validate_automation(ok, max_actions=2)
    assert a.definition.trigger.keywords == ["promo"]
    with pytest.raises(ValueError):
        automations.validate_automation(ok, max_actions=1)
    with pytest.raises(ValidationError):  # pas de mot-clé
        automations.validate_automation({"name": "x", "definition": {"trigger": {"type": "message_contains"},
                                                                    "actions": [{"type": "delete_message"}]}}, max_actions=5)
    with pytest.raises(ValidationError):  # rôle manquant
        automations.validate_automation({"name": "x", "definition": {"trigger": {"type": "member_join"},
                                                                    "actions": [{"type": "add_role"}]}}, max_actions=5)
    with pytest.raises(ValidationError):  # timeout > 28 jours
        automations.validate_automation({"name": "x", "definition": {"trigger": {"type": "member_join"},
                                                                    "actions": [{"type": "timeout", "minutes": 50000}]}}, max_actions=5)
    with pytest.raises(ValidationError):  # type d'action inexistant (aucune exécution arbitraire)
        automations.validate_automation({"name": "x", "definition": {"trigger": {"type": "member_join"},
                                                                    "actions": [{"type": "exec", "text": "import os"}]}}, max_actions=5)
    good = {"name": "x", "definition": {"trigger": {"type": "member_join"},
                                        "actions": [{"type": "add_role", "role_id": ROLE_A, "delay_seconds": 60}]}}
    assert automations.validate_automation(good, max_actions=1).definition.cooldown_seconds == 10


# ----------------------------------------------------------------- Premium
def test_premium_grace_and_expiry(clean_guild_data):
    with session_scope() as db:
        db.add(Subscription(guild_id=GUILD_A, status="past_due", source="stripe",
                            current_period_end=utcnow() - timedelta(days=1)))
    with session_scope() as db:
        assert premium.is_premium(db, GUILD_A)  # période de grâce de 3 jours
        sub = premium.get_subscription(db, GUILD_A)
        sub.current_period_end = utcnow() - timedelta(days=5)
    with session_scope() as db:
        assert not premium.is_premium(db, GUILD_A)
        assert premium.expire_overdue(db) == 1


def test_dev_premium_refused_in_production(monkeypatch, clean_guild_data):
    from nexbot.core import config

    prod = config.Settings(**{**config.get_settings().__dict__, "env": "production", "premium_dev_mode": False})
    monkeypatch.setattr(premium, "get_settings", lambda: prod)
    with session_scope() as db, pytest.raises(PermissionError):
        premium.grant_dev_premium(db, GUILD_A, OWNER_ID)


def test_production_forces_dev_mode_off(monkeypatch):
    from nexbot.core import config

    monkeypatch.setenv("NEXBOT_ENV", "production")
    monkeypatch.setenv("PREMIUM_DEV_MODE", "true")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().premium_dev_mode is False
    finally:
        monkeypatch.setenv("NEXBOT_ENV", "development")
        config.get_settings.cache_clear()


# ----------------------------------------------------------------- Stripe
def _sub_obj(status="active", end_days=30, sid="sub_123"):
    return {"id": sid, "object": "subscription", "status": status, "customer": "cus_1",
            "current_period_end": int(time.time()) + end_days * 86400, "cancel_at_period_end": False,
            "metadata": {"guild_id": str(GUILD_A), "user_id": "300000000000000001"}}


def test_stripe_events_sync_premium_rights(clean_guild_data):
    with session_scope() as db:
        assert billing.handle_event(db, {"id": "evt_1", "type": "checkout.session.completed",
                                         "data": {"object": {"subscription": "sub_123"}}},
                                    fetch_subscription=lambda sid: _sub_obj()) == "ok"
    with session_scope() as db:
        assert premium.is_premium(db, GUILD_A)
        assert billing.handle_event(db, {"id": "evt_1", "type": "checkout.session.completed",
                                         "data": {"object": {}}}) == "duplicate"
    with session_scope() as db:
        billing.handle_event(db, {"id": "evt_2", "type": "invoice.payment_failed",
                                  "data": {"object": {"subscription": "sub_123"}}},
                             fetch_subscription=lambda sid: _sub_obj(status="past_due"))
        assert premium.get_subscription(db, GUILD_A).status == "past_due"
    with session_scope() as db:
        billing.handle_event(db, {"id": "evt_3", "type": "customer.subscription.deleted",
                                  "data": {"object": _sub_obj(status="canceled", end_days=-10)}})
    with session_scope() as db:
        assert not premium.is_premium(db, GUILD_A)
        assert db.query(Subscription).count() == 1  # mise à jour, pas de doublon


def test_stripe_webhook_signature(monkeypatch, make_client, clean_guild_data):
    from nexbot.core import config

    cfg = config.Settings(**{**config.get_settings().__dict__, "stripe_secret_key": "sk_test_x",
                             "stripe_webhook_secret": "whsec_test", "stripe_price_id": "price_x"})
    monkeypatch.setattr(billing, "get_settings", lambda: cfg)
    client = make_client()
    payload = json.dumps({"id": "evt_sig", "object": "event", "type": "customer.subscription.updated",
                          "data": {"object": _sub_obj(sid="sub_sig")}}).encode()
    bad = client.post("/api/stripe/webhook", content=payload, headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert bad.status_code == 400
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    ok = client.post("/api/stripe/webhook", content=payload, headers={"stripe-signature": f"t={ts},v1={sig}"})
    assert ok.status_code == 200, ok.text
    with session_scope() as db:
        assert premium.is_premium(db, GUILD_A)
