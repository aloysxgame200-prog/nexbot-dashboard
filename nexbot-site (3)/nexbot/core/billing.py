"""Intégration Stripe (abonnements Premium par serveur).

Tant que STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET / STRIPE_PRICE_ID ne sont pas
définis, le paiement est désactivé et seul le Premium de développement existe.
Les droits Premium ne sont accordés QUE par les webhooks signés de Stripe.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import StripeEvent, Subscription

STATUS_MAP = {"active": "active", "trialing": "trialing", "past_due": "past_due", "unpaid": "past_due",
              "canceled": "canceled", "incomplete": "past_due", "incomplete_expired": "expired",
              "paused": "canceled"}


class BillingDisabled(RuntimeError):
    pass


def _stripe():
    s = get_settings()
    if not s.stripe_enabled:
        raise BillingDisabled("Les paiements ne sont pas encore configurés.")
    import stripe

    stripe.api_key = s.stripe_secret_key
    return stripe


def create_checkout(guild_id: int, user_id: int) -> str:
    stripe = _stripe()
    s = get_settings()
    base = s.dashboard_url.rstrip("/")
    meta = {"guild_id": str(guild_id), "user_id": str(user_id)}
    checkout = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": s.stripe_price_id, "quantity": 1}],
        success_url=f"{base}/g/{guild_id}/premium?checkout=success",
        cancel_url=f"{base}/g/{guild_id}/premium?checkout=cancel",
        client_reference_id=str(guild_id),
        metadata=meta,
        subscription_data={"metadata": meta},
        allow_promotion_codes=True,
    )
    return checkout.url


def create_portal(customer_id: str, guild_id: int) -> str:
    stripe = _stripe()
    base = get_settings().dashboard_url.rstrip("/")
    portal = stripe.billing_portal.Session.create(customer=customer_id, return_url=f"{base}/g/{guild_id}/premium")
    return portal.url


def verify_event(payload: bytes, signature: str) -> Any:
    """Lève une exception si la signature Stripe est invalide."""
    stripe = _stripe()
    return stripe.Webhook.construct_event(payload, signature, get_settings().stripe_webhook_secret)


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None


def _get(obj: Any, key: str, default=None):
    try:
        return obj[key] if key in obj else default
    except TypeError:
        return getattr(obj, key, default)


def _period_end(sub_obj: Any) -> datetime | None:
    end = _get(sub_obj, "current_period_end")
    if end is None:  # API Stripe récente : la période est portée par les items
        items = _get(_get(sub_obj, "items", {}) or {}, "data", []) or []
        ends = [_get(i, "current_period_end") for i in items if _get(i, "current_period_end")]
        end = max(ends) if ends else None
    return _ts(end)


def _upsert_from_subscription(db: Session, sub_obj: Any) -> Subscription | None:
    meta = _get(sub_obj, "metadata", {}) or {}
    sid = _get(sub_obj, "id")
    row = db.scalar(select(Subscription).where(Subscription.stripe_subscription_id == sid))
    if row is None:
        gid = _get(meta, "guild_id")
        if not gid or not str(gid).isdigit():
            return None
        row = Subscription(guild_id=int(gid), stripe_subscription_id=sid, source="stripe",
                           purchaser_id=int(_get(meta, "user_id")) if str(_get(meta, "user_id", "")).isdigit() else None)
        db.add(row)
    row.stripe_customer_id = _get(sub_obj, "customer")
    row.status = STATUS_MAP.get(_get(sub_obj, "status", ""), "expired")
    row.current_period_end = _period_end(sub_obj)
    row.cancel_at_period_end = bool(_get(sub_obj, "cancel_at_period_end", False))
    return row


def handle_event(db: Session, event: Any, fetch_subscription=None) -> str:
    """Applique un événement Stripe vérifié. Idempotent."""
    event_id, etype = _get(event, "id"), _get(event, "type")
    if db.get(StripeEvent, event_id):
        return "duplicate"
    obj = _get(_get(event, "data"), "object")

    def fetch(sid: str):
        if fetch_subscription:
            return fetch_subscription(sid)
        return _stripe().Subscription.retrieve(sid)

    if etype == "checkout.session.completed" and _get(obj, "subscription"):
        _upsert_from_subscription(db, fetch(_get(obj, "subscription")))
    elif etype in {"customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted"}:
        row = _upsert_from_subscription(db, obj)
        if row and etype == "customer.subscription.deleted":
            row.status = "canceled"
    elif etype in {"invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"}:
        sid = _get(obj, "subscription") or _get(_get(_get(obj, "parent", {}) or {}, "subscription_details", {}) or {},
                                               "subscription")
        if sid:
            row = _upsert_from_subscription(db, fetch(sid))
            if row and etype == "invoice.payment_failed":
                row.status = "past_due"
    db.add(StripeEvent(id=event_id, type=etype))
    return "ok"
