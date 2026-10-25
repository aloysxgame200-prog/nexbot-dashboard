"""File d'actions Dashboard → bot et statistiques partagées."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BotJob, DailyStat, Heartbeat, utcnow

JOB_KINDS = {
    "send_message",       # envoyer un message / embed depuis l'éditeur
    "post_ticket_panel",  # poster le panneau des tickets
    "test_automation",    # exécuter une automatisation en mode test
    "refresh_guild",      # resynchroniser les infos du serveur
}
MAX_PENDING_PER_GUILD = 10


class JobError(ValueError):
    pass


def enqueue(session: Session, guild_id: int, kind: str, payload: dict, user_id: int) -> BotJob:
    if kind not in JOB_KINDS:
        raise JobError("Action inconnue")
    pending = session.scalar(select(func.count()).select_from(BotJob)
                             .where(BotJob.guild_id == guild_id, BotJob.status.in_(["pending", "running"])))
    if pending >= MAX_PENDING_PER_GUILD:
        raise JobError("Trop d'actions en attente pour ce serveur, réessayez dans quelques secondes.")
    job = BotJob(guild_id=guild_id, kind=kind, payload=payload, created_by=user_id)
    session.add(job)
    session.flush()
    return job


def claim_pending(session: Session, limit: int = 20) -> list[BotJob]:
    """Utilisé par le bot : récupère les actions en attente et les marque « en cours »."""
    jobs = session.scalars(select(BotJob).where(BotJob.status == "pending").order_by(BotJob.id).limit(limit)
                           .with_for_update(skip_locked=True)).all()
    for j in jobs:
        j.status = "running"
    return list(jobs)


def finish(session: Session, job_id: int, ok: bool, result: str = "") -> None:
    job = session.get(BotJob, job_id)
    if job:
        job.status = "done" if ok else "failed"
        job.result = result[:1000]
        job.processed_at = utcnow()


# ----------------------------------------------------------------------------- statistiques
def incr(session: Session, guild_id: int, metric: str, n: int = 1, day: date | None = None) -> None:
    day = day or utcnow().date()
    row = session.get(DailyStat, {"guild_id": guild_id, "day": day, "metric": metric})
    if row is None:
        session.add(DailyStat(guild_id=guild_id, day=day, metric=metric, value=n))
    else:
        row.value += n


def series(session: Session, guild_id: int, metrics: list[str], days: int) -> dict:
    today = utcnow().date()
    start = today - timedelta(days=days - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    data = {m: [0] * days for m in metrics}
    rows = session.execute(select(DailyStat.day, DailyStat.metric, DailyStat.value)
                           .where(DailyStat.guild_id == guild_id, DailyStat.day >= start,
                                  DailyStat.metric.in_(metrics))).all()
    for d, m, v in rows:
        idx = (d - start).days
        if 0 <= idx < days:
            data[m][idx] += v
    return {"labels": labels, "series": data}


# ----------------------------------------------------------------------------- état du bot
def heartbeat(session: Session, component: str, info: dict) -> None:
    row = session.get(Heartbeat, component)
    if row is None:
        session.add(Heartbeat(component=component, info=info))
    else:
        row.last_seen = utcnow()
        row.info = info


def bot_status(session: Session) -> dict:
    row = session.get(Heartbeat, "bot")
    if row is None:
        return {"online": False, "last_seen": None, "info": {}}
    last = row.last_seen if row.last_seen.tzinfo else row.last_seen.replace(tzinfo=utcnow().tzinfo)
    return {"online": (utcnow() - last).total_seconds() < 120, "last_seen": last.isoformat(), "info": row.info}
