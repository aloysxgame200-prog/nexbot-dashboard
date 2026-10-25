"""Tables partagées par le bot et le Dashboard (une seule source de vérité)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, IdType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


TZ = DateTime(timezone=True)


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)


class Guild(Base):
    """Serveurs connus. Écrit par le bot (join/leave/update)."""

    __tablename__ = "guilds"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100), default="")
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    bot_present: Mapped[bool] = mapped_column(Boolean, default=True)
    joined_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, onupdate=utcnow)


class GuildSettings(Base):
    """Paramètres d'un module pour un serveur (JSON validé par nexbot.core.schemas)."""

    __tablename__ = "guild_settings"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, onupdate=utcnow, index=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    purchaser_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    plan: Mapped[str] = mapped_column(String(32), default="premium")
    # active | trialing | past_due | canceled | expired
    status: Mapped[str] = mapped_column(String(16), default="active")
    source: Mapped[str] = mapped_column(String(16), default="stripe")  # stripe | dev | manual
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    current_period_end: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, onupdate=utcnow)


class StripeEvent(Base):
    """Idempotence des webhooks Stripe."""

    __tablename__ = "stripe_events"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    type: Mapped[str] = mapped_column(String(80))
    processed_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)


class Sanction(Base):
    __tablename__ = "sanctions"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_id: Mapped[int] = mapped_column(BigInteger)
    # warn | timeout | kick | ban | unban | tempban | untimeout
    type: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ModNote(Base):
    __tablename__ = "mod_notes"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)


class DailyStat(Base):
    """Compteurs agrégés par jour : seules des mesures réellement enregistrées."""

    __tablename__ = "daily_stats"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class SecurityIncident(Base):
    __tablename__ = "security_incidents"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(12), default="medium")  # low|medium|high|critical
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    action_taken: Mapped[str] = mapped_column(String(40), default="alert")
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    opener_id: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(64), default="Support")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | closed
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|accepted|refused
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    staff_comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger)
    target_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    message_link: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | handled | dismissed
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)


class Automation(Base):
    __tablename__ = "automations"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)  # validé par core.automations
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, onupdate=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)


class ScheduledMessage(Base):
    """Annonces / messages programmés (Premium)."""

    __tablename__ = "scheduled_messages"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(80))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message: Mapped[dict] = mapped_column(JSON, default=dict)  # {content, embed}
    interval_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = une seule fois
    next_run_at: Mapped[datetime] = mapped_column(TZ, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class BotJob(Base):
    """File d'actions Dashboard → bot (envoyer un embed, poster un panneau de tickets…)."""

    __tablename__ = "bot_jobs"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    result: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class GlobalState(Base):
    """État global (maintenance, présence…). Écrit UNIQUEMENT par les commandes `*` du bot."""

    __tablename__ = "global_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, onupdate=utcnow)


class Heartbeat(Base):
    """Le bot signale sa présence : le Dashboard affiche son état réel."""

    __tablename__ = "heartbeats"
    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    info: Mapped[dict] = mapped_column(JSON, default=dict)


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 du jeton du cookie
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str | None] = mapped_column(String(64), nullable=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime] = mapped_column(TZ)
    csrf_token: Mapped[str] = mapped_column(String(64))
    guilds_cache: Mapped[list] = mapped_column(JSON, default=list)
    guilds_cached_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZ, index=True)


class DashboardAudit(Base):
    """Journal des modifications faites depuis le Dashboard."""

    __tablename__ = "dashboard_audit"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, index=True)


class ActiveUser(Base):
    """Utilisateurs ayant utilisé Nexbot (commande, bouton, ticket…) un jour donné.

    Sert uniquement à compter les utilisateurs actifs (jour / semaine / mois) pour
    la commande propriétaire *stats. Aucun contenu n'est stocké ; les lignes de
    plus de 90 jours sont supprimées automatiquement.
    """

    __tablename__ = "active_users"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    day: Mapped[date] = mapped_column(Date, primary_key=True)


class CommandUsage(Base):
    """Nombre d'utilisations de chaque commande par jour (tous serveurs confondus)."""

    __tablename__ = "command_usage"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    command: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class GlobalBlacklist(Base):
    """Utilisateurs ou serveurs bannis de Nexbot par le propriétaire (commandes *)."""

    __tablename__ = "global_blacklist"
    target_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(String(8), default="user")  # user | guild
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow)


class GuildEvent(Base):
    """Arrivées / départs de Nexbot sur des serveurs (croissance, pour *stats)."""

    __tablename__ = "guild_events"
    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(8))  # join | leave
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TZ, default=utcnow, index=True)


Index("ix_daily_stats_guild_day", DailyStat.guild_id, DailyStat.day)
