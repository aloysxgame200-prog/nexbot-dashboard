"""Droits Premium, toujours vérifiés côté serveur à partir de la base de données."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Subscription, utcnow

ACTIVE_STATUSES = {"active", "trialing", "past_due"}
GRACE_PERIOD = timedelta(days=3)  # tolérance après un paiement échoué / fin de période

LIMITS = {
    "free": {"automations": 3, "automation_actions": 2, "scheduled_messages": 0,
             "history_days": 30, "stats_days": 7},
    "premium": {"automations": 25, "automation_actions": 5, "scheduled_messages": 20,
                "history_days": 3650, "stats_days": 90},
}

FEATURES = [
    # (fonctionnalité, gratuit, premium)
    ("Modération complète (ban, kick, timeout, warn, clear, notes)", True, True),
    ("Anti-raid et anti-nuke (protections essentielles)", True, True),
    ("Anti-spam, filtres de liens, d'invitations et de mots", True, True),
    ("Logs par catégorie", True, True),
    ("Messages de bienvenue et de départ (texte)", True, True),
    ("Tickets, suggestions et signalements", True, True),
    ("Dashboard complet", True, True),
    ("Mots interdits", "50", "1000"),
    ("Rôles automatiques", "2", "10"),
    ("Automatisations", "3 (2 actions)", "25 (5 actions)"),
    ("Historique des sanctions affiché", "30 jours", "Illimité"),
    ("Statistiques", "7 jours", "90 jours, personnalisables"),
    ("Conservation des données", "30 jours", "Jusqu'à 365 jours"),
    ("Éditeur visuel d'embeds et annonces programmées", False, True),
    ("Embeds de bienvenue, de départ et de tickets", False, True),
    ("Messages de modération personnalisés", False, True),
    ("Couleurs et apparence des logs", False, True),
    ("Anti-raid avancé (récidives, restauration, alertes MP)", False, True),
    ("Catégories de tickets et fermeture automatique", False, True),
    ("Thème personnalisé du Dashboard", False, True),
]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def subscription_is_active(sub: Subscription, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if sub.status not in ACTIVE_STATUSES:
        return False
    end = _aware(sub.current_period_end)
    if end is None:
        return sub.source == "manual"  # attribution manuelle sans échéance
    return now <= end + GRACE_PERIOD


def get_subscription(session: Session, guild_id: int) -> Subscription | None:
    """Abonnement le plus pertinent (actif d'abord, puis le plus récent)."""
    subs = session.scalars(select(Subscription).where(Subscription.guild_id == guild_id)
                           .order_by(Subscription.updated_at.desc())).all()
    for sub in subs:
        if subscription_is_active(sub):
            return sub
    return subs[0] if subs else None


def is_premium(session: Session, guild_id: int) -> bool:
    sub = get_subscription(session, guild_id)
    return bool(sub and subscription_is_active(sub))


def limits(premium: bool) -> dict:
    return LIMITS["premium" if premium else "free"]


def grant_dev_premium(session: Session, guild_id: int, user_id: int, days: int = 30) -> Subscription:
    """Premium de test, uniquement en mode développement. L'appelant vérifie is_owner()."""
    if not get_settings().premium_dev_mode:
        raise PermissionError("Le Premium de développement est désactivé dans cet environnement.")
    days = max(1, min(int(days), 365))
    sub = Subscription(guild_id=guild_id, purchaser_id=user_id, plan="premium", status="active", source="dev",
                       current_period_end=utcnow() + timedelta(days=days))
    session.add(sub)
    session.flush()
    return sub


def revoke_dev_premium(session: Session, guild_id: int) -> int:
    if not get_settings().premium_dev_mode:
        raise PermissionError("Le Premium de développement est désactivé dans cet environnement.")
    n = 0
    for sub in session.scalars(select(Subscription).where(Subscription.guild_id == guild_id,
                                                          Subscription.source == "dev")):
        if sub.status != "expired":
            sub.status = "expired"
            sub.current_period_end = utcnow() - GRACE_PERIOD - timedelta(seconds=1)
            n += 1
    return n


def expire_overdue(session: Session) -> int:
    """Tâche périodique : marque comme expirés les abonnements échus. Les paramètres sont conservés."""
    n = 0
    now = utcnow()
    for sub in session.scalars(select(Subscription).where(Subscription.status.in_(ACTIVE_STATUSES))):
        if not subscription_is_active(sub, now):
            sub.status = "expired"
            n += 1
    return n
