"""API JSON du Dashboard. Chaque route de serveur passe par auth.require_guild()."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from ...core import automations as auto_mod
from ...core import billing, jobs
from ...core import premium as premium_mod
from ...core import settings_store
from ...core.config import get_settings
from ...core.db import session_scope
from ...core.embeds import VARIABLES, MessageModel, TemplateError
from ...core.models import (Automation, DashboardAudit, ModNote, Report, Sanction, ScheduledMessage,
                            SecurityIncident, Suggestion, Ticket, utcnow)
from ...core.schemas import MODULES, STAT_METRICS, describe_module
from ...core.security import is_owner
from .. import auth, discord_api, ratelimit

router = APIRouter(prefix="/api")


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, ValidationError):
        errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": str(e["msg"]).removeprefix("Value error, ")}
                for e in exc.errors()]
        return HTTPException(422, {"message": errs[0]["message"] if errs else "Données invalides", "errors": errs})
    return HTTPException(422, {"message": str(exc), "errors": []})


def _mutate_limit(ctx: auth.GuildContext) -> None:
    ratelimit.hit(f"mut:{ctx.user.id}", 60, 60)


def _audit(db, ctx: auth.GuildContext, action: str, details: dict | None = None) -> None:
    db.add(DashboardAudit(guild_id=ctx.id, user_id=ctx.user.id, action=action, details=details or {}))


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


# ------------------------------------------------------------------ utilisateur
@router.get("/me")
async def me(request: Request):
    user = auth.require_user(request)
    return {"id": str(user.id), "name": user.username, "avatar": user.avatar_url}


# ------------------------------------------------------------------ vue d'ensemble
@router.get("/guilds/{guild_id}/overview")
async def overview(request: Request, guild_id: int, days: int = 7):
    ctx = await auth.require_guild(request, guild_id)
    lim = premium_mod.limits(ctx.premium)
    days = max(1, min(days, lim["stats_days"]))
    with session_scope() as db:
        stats_pref = settings_store.effective(db, guild_id, "stats", ctx.premium)
        metrics = stats_pref.visible_metrics or ["joins", "leaves", "messages", "sanctions"]
        since = utcnow() - timedelta(days=days)
        counts = {
            "sanctions": db.scalar(select(func.count()).select_from(Sanction)
                                   .where(Sanction.guild_id == guild_id, Sanction.created_at >= since)),
            "incidents_open": db.scalar(select(func.count()).select_from(SecurityIncident)
                                        .where(SecurityIncident.guild_id == guild_id,
                                               SecurityIncident.resolved.is_(False))),
            "tickets_open": db.scalar(select(func.count()).select_from(Ticket)
                                      .where(Ticket.guild_id == guild_id, Ticket.status == "open")),
            "reports_open": db.scalar(select(func.count()).select_from(Report)
                                      .where(Report.guild_id == guild_id, Report.status == "open")),
            "automations": db.scalar(select(func.count()).select_from(Automation)
                                     .where(Automation.guild_id == guild_id, Automation.enabled.is_(True))),
        }
        enabled = {}
        for key in ("moderation", "antiraid", "welcome", "autoroles", "tickets", "suggestions", "reports"):
            enabled[key] = bool(getattr(settings_store.effective(db, guild_id, key, ctx.premium), "enabled", False))
        logs = settings_store.effective(db, guild_id, "logs", ctx.premium)
        enabled["logs"] = any(getattr(logs, f"{k}_enabled") for k in
                              ("members", "messages", "moderation", "roles", "channels", "permissions",
                               "antiraid", "security", "errors"))
        series = jobs.series(db, guild_id, metrics, days)
        status = jobs.bot_status(db)
    return {"counts": counts, "modules": enabled, "stats": series, "days": days, "max_days": lim["stats_days"],
            "metric_labels": dict(STAT_METRICS), "chart_style": stats_pref.chart_style, "bot": status}


# ------------------------------------------------------------------ données Discord (salons, rôles)
@router.get("/guilds/{guild_id}/discord")
async def discord_data(request: Request, guild_id: int):
    await auth.require_guild(request, guild_id)
    try:
        channels = await discord_api.guild_channels(guild_id)
        roles = await discord_api.guild_roles(guild_id)
    except discord_api.DiscordError as e:
        raise HTTPException(502, f"Impossible de lire les salons et rôles ({e.status}).")
    return {"channels": channels, "roles": roles}


# ------------------------------------------------------------------ modules
@router.get("/guilds/{guild_id}/modules/{module}")
async def get_module(request: Request, guild_id: int, module: str):
    if module not in MODULES:
        raise HTTPException(404, "Module inconnu")
    ctx = await auth.require_guild(request, guild_id)
    with session_scope() as db:
        values = settings_store.stored(db, guild_id, module).model_dump(mode="json")
    spec = MODULES[module]
    return {"module": module, "title": spec.title, "description": spec.description,
            "premium_only": spec.premium_only, "premium": ctx.premium,
            "schema": describe_module(module), "values": values, "variables": VARIABLES}


@router.put("/guilds/{guild_id}/modules/{module}")
async def put_module(request: Request, guild_id: int, module: str, payload: dict = Body(...)):
    if module not in MODULES:
        raise HTTPException(404, "Module inconnu")
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _mutate_limit(ctx)
    try:
        with session_scope() as db:
            new = settings_store.save(db, guild_id, module, payload, user_id=ctx.user.id, premium=ctx.premium)
    except settings_store.PremiumRequired as e:
        raise HTTPException(402, {"message": str(e), "errors": e.errors})
    except settings_store.SettingsError as e:
        raise HTTPException(422, {"message": str(e), "errors": e.errors})
    return {"ok": True, "values": new.model_dump(mode="json")}


# ------------------------------------------------------------------ sanctions & notes
@router.get("/guilds/{guild_id}/sanctions")
async def list_sanctions(request: Request, guild_id: int, user_id: str = "", type: str = "",
                         page: int = Query(1, ge=1, le=10000)):
    ctx = await auth.require_guild(request, guild_id)
    days = premium_mod.limits(ctx.premium)["history_days"]
    per = 25
    with session_scope() as db:
        q = select(Sanction).where(Sanction.guild_id == guild_id,
                                   Sanction.created_at >= utcnow() - timedelta(days=days))
        if user_id.isdigit():
            q = q.where(Sanction.user_id == int(user_id))
        if type:
            q = q.where(Sanction.type == type)
        total = db.scalar(select(func.count()).select_from(q.subquery()))
        rows = db.scalars(q.order_by(Sanction.id.desc()).offset((page - 1) * per).limit(per)).all()
        items = [{"id": r.id, "user_id": str(r.user_id), "moderator_id": str(r.moderator_id), "type": r.type,
                  "reason": r.reason, "automatic": r.automatic, "created_at": _iso(r.created_at),
                  "expires_at": _iso(r.expires_at), "active": r.active} for r in rows]
    return {"items": items, "total": total, "page": page, "per_page": per, "history_days": days}


@router.post("/guilds/{guild_id}/sanctions/{sanction_id}/revoke")
async def revoke_warn(request: Request, guild_id: int, sanction_id: int):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _mutate_limit(ctx)
    with session_scope() as db:
        s = db.get(Sanction, sanction_id)
        if s is None or s.guild_id != guild_id:
            raise HTTPException(404, "Sanction introuvable")
        if s.type != "warn":
            raise HTTPException(422, {"message": "Seuls les avertissements se retirent depuis le Dashboard. "
                                                 "Utilisez /unban ou /untimeout sur Discord pour les autres sanctions.",
                                      "errors": []})
        s.active, s.revoked_at, s.revoked_by = False, utcnow(), ctx.user.id
        _audit(db, ctx, "sanction.revoke", {"id": sanction_id})
    return {"ok": True}


class NoteIn(BaseModel):
    user_id: str = Field(pattern=r"^\d{15,21}$")
    content: str = Field(min_length=1, max_length=1500)


@router.get("/guilds/{guild_id}/notes")
async def list_notes(request: Request, guild_id: int, user_id: str = Query(pattern=r"^\d{15,21}$")):
    await auth.require_guild(request, guild_id)
    with session_scope() as db:
        rows = db.scalars(select(ModNote).where(ModNote.guild_id == guild_id, ModNote.user_id == int(user_id))
                          .order_by(ModNote.id.desc()).limit(100)).all()
        return {"items": [{"id": n.id, "author_id": str(n.author_id), "content": n.content,
                           "created_at": _iso(n.created_at)} for n in rows]}


@router.post("/guilds/{guild_id}/notes")
async def add_note(request: Request, guild_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _mutate_limit(ctx)
    try:
        note = NoteIn.model_validate(payload)
    except ValidationError as e:
        raise _err(e)
    with session_scope() as db:
        db.add(ModNote(guild_id=guild_id, user_id=int(note.user_id), author_id=ctx.user.id, content=note.content))
        _audit(db, ctx, "note.add", {"user_id": note.user_id})
    return {"ok": True}


# ------------------------------------------------------------------ incidents de sécurité
@router.get("/guilds/{guild_id}/incidents")
async def list_incidents(request: Request, guild_id: int):
    ctx = await auth.require_guild(request, guild_id)
    days = premium_mod.limits(ctx.premium)["history_days"]
    with session_scope() as db:
        rows = db.scalars(select(SecurityIncident).where(SecurityIncident.guild_id == guild_id,
                                                         SecurityIncident.created_at >= utcnow() - timedelta(days=days))
                          .order_by(SecurityIncident.id.desc()).limit(200)).all()
        return {"items": [{"id": i.id, "kind": i.kind, "severity": i.severity, "actor_id": str(i.actor_id or ""),
                           "details": i.details, "action_taken": i.action_taken, "created_at": _iso(i.created_at),
                           "resolved": i.resolved} for i in rows]}


@router.post("/guilds/{guild_id}/incidents/{incident_id}/resolve")
async def resolve_incident(request: Request, guild_id: int, incident_id: int):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    with session_scope() as db:
        inc = db.get(SecurityIncident, incident_id)
        if inc is None or inc.guild_id != guild_id:
            raise HTTPException(404, "Incident introuvable")
        inc.resolved, inc.resolved_by = True, ctx.user.id
        _audit(db, ctx, "incident.resolve", {"id": incident_id})
    return {"ok": True}


# ------------------------------------------------------------------ tickets / suggestions / signalements
@router.get("/guilds/{guild_id}/records/{kind}")
async def list_records(request: Request, guild_id: int, kind: str, status: str = ""):
    await auth.require_guild(request, guild_id)
    model = {"tickets": Ticket, "suggestions": Suggestion, "reports": Report}.get(kind)
    if model is None:
        raise HTTPException(404, "Type inconnu")
    with session_scope() as db:
        q = select(model).where(model.guild_id == guild_id)
        if status:
            q = q.where(model.status == status)
        rows = db.scalars(q.order_by(model.id.desc()).limit(200)).all()
        items = []
        for r in rows:
            d = {c.name: getattr(r, c.name) for c in model.__table__.columns}
            items.append({k: (str(v) if isinstance(v, int) and k.endswith("_id") and v else
                              _iso(v) if hasattr(v, "isoformat") else v) for k, v in d.items()})
    return {"items": items}


@router.post("/guilds/{guild_id}/records/reports/{report_id}")
async def update_report(request: Request, guild_id: int, report_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    status = payload.get("status")
    if status not in {"open", "handled", "dismissed"}:
        raise HTTPException(422, {"message": "Statut invalide", "errors": []})
    with session_scope() as db:
        r = db.get(Report, report_id)
        if r is None or r.guild_id != guild_id:
            raise HTTPException(404, "Signalement introuvable")
        r.status, r.handled_by = status, ctx.user.id
        _audit(db, ctx, "report.status", {"id": report_id, "status": status})
    return {"ok": True}


@router.post("/guilds/{guild_id}/tickets/panel")
async def post_ticket_panel(request: Request, guild_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    ratelimit.hit(f"job:{guild_id}", 10, 60)
    channel_id = str(payload.get("channel_id", ""))
    if not channel_id.isdigit():
        raise HTTPException(422, {"message": "Choisissez le salon du panneau", "errors": []})
    with session_scope() as db:
        if not settings_store.effective(db, guild_id, "tickets", ctx.premium).enabled:
            raise HTTPException(422, {"message": "Activez d'abord le module Tickets et enregistrez.", "errors": []})
        job = jobs.enqueue(db, guild_id, "post_ticket_panel", {"channel_id": channel_id}, ctx.user.id)
        return {"ok": True, "job_id": job.id}


# ------------------------------------------------------------------ éditeur de messages (Premium)
def _require_premium(ctx: auth.GuildContext) -> None:
    if not ctx.premium:
        raise HTTPException(402, {"message": "Fonctionnalité Premium.", "errors": []})


@router.post("/guilds/{guild_id}/messages/validate")
async def validate_message(request: Request, guild_id: int, payload: dict = Body(...)):
    await auth.require_guild(request, guild_id)
    context = payload.get("context", "generic")
    try:
        MessageModel.model_validate(payload.get("message") or {}).check(context)
    except (ValidationError, TemplateError, ValueError) as e:
        raise _err(e)
    return {"ok": True}


@router.post("/guilds/{guild_id}/messages/send")
async def send_message(request: Request, guild_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _require_premium(ctx)
    ratelimit.hit(f"job:{guild_id}", 10, 60)
    channel_id = str(payload.get("channel_id", ""))
    if not channel_id.isdigit():
        raise HTTPException(422, {"message": "Choisissez un salon", "errors": []})
    try:
        msg = MessageModel.model_validate(payload.get("message") or {}).check("announcement")
    except (ValidationError, TemplateError, ValueError) as e:
        raise _err(e)
    with session_scope() as db:
        try:
            job = jobs.enqueue(db, guild_id, "send_message",
                               {"channel_id": channel_id, "message": msg.model_dump(mode="json")}, ctx.user.id)
        except jobs.JobError as e:
            raise HTTPException(429, str(e))
        _audit(db, ctx, "message.send", {"channel_id": channel_id})
        return {"ok": True, "job_id": job.id}


@router.get("/guilds/{guild_id}/jobs/{job_id}")
async def job_status(request: Request, guild_id: int, job_id: int):
    await auth.require_guild(request, guild_id)
    from ...core.models import BotJob

    with session_scope() as db:
        job = db.get(BotJob, job_id)
        if job is None or job.guild_id != guild_id:
            raise HTTPException(404, "Action introuvable")
        return {"status": job.status, "result": job.result}


# ------------------------------------------------------------------ annonces programmées (Premium)
class ScheduledIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    channel_id: str = Field(pattern=r"^\d{15,21}$")
    message: MessageModel
    interval_minutes: int = Field(0, ge=0, le=525600)
    start_in_minutes: int = Field(0, ge=0, le=525600)
    enabled: bool = True

    def checked(self) -> "ScheduledIn":
        if 0 < self.interval_minutes < 30:
            raise ValueError("Intervalle minimum : 30 minutes (protection anti-spam)")
        self.message.check("announcement")
        return self


@router.get("/guilds/{guild_id}/scheduled")
async def list_scheduled(request: Request, guild_id: int):
    await auth.require_guild(request, guild_id)
    with session_scope() as db:
        rows = db.scalars(select(ScheduledMessage).where(ScheduledMessage.guild_id == guild_id)
                          .order_by(ScheduledMessage.id)).all()
        return {"items": [{"id": r.id, "name": r.name, "channel_id": str(r.channel_id), "message": r.message,
                           "interval_minutes": r.interval_minutes, "next_run_at": _iso(r.next_run_at),
                           "enabled": r.enabled, "last_run_at": _iso(r.last_run_at)} for r in rows]}


@router.post("/guilds/{guild_id}/scheduled")
async def create_scheduled(request: Request, guild_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _require_premium(ctx)
    _mutate_limit(ctx)
    try:
        item = ScheduledIn.model_validate(payload).checked()
    except (ValidationError, TemplateError, ValueError) as e:
        raise _err(e)
    with session_scope() as db:
        n = db.scalar(select(func.count()).select_from(ScheduledMessage).where(ScheduledMessage.guild_id == guild_id))
        if n >= premium_mod.limits(True)["scheduled_messages"]:
            raise HTTPException(422, {"message": "Limite d'annonces programmées atteinte", "errors": []})
        row = ScheduledMessage(guild_id=guild_id, name=item.name, channel_id=int(item.channel_id),
                               message=item.message.model_dump(mode="json"), interval_minutes=item.interval_minutes,
                               next_run_at=utcnow() + timedelta(minutes=max(1, item.start_in_minutes)),
                               enabled=item.enabled, created_by=ctx.user.id)
        db.add(row)
        db.flush()
        _audit(db, ctx, "scheduled.create", {"id": row.id})
        return {"ok": True, "id": row.id}


@router.delete("/guilds/{guild_id}/scheduled/{item_id}")
async def delete_scheduled(request: Request, guild_id: int, item_id: int):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    with session_scope() as db:
        row = db.get(ScheduledMessage, item_id)
        if row is None or row.guild_id != guild_id:
            raise HTTPException(404, "Annonce introuvable")
        db.delete(row)
        _audit(db, ctx, "scheduled.delete", {"id": item_id})
    return {"ok": True}


@router.post("/guilds/{guild_id}/scheduled/{item_id}/toggle")
async def toggle_scheduled(request: Request, guild_id: int, item_id: int):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    with session_scope() as db:
        row = db.get(ScheduledMessage, item_id)
        if row is None or row.guild_id != guild_id:
            raise HTTPException(404, "Annonce introuvable")
        if not row.enabled:
            _require_premium(ctx)
        row.enabled = not row.enabled
        if row.enabled and row.next_run_at.replace(tzinfo=utcnow().tzinfo) < utcnow():
            row.next_run_at = utcnow() + timedelta(minutes=1)
        return {"ok": True, "enabled": row.enabled}


# ------------------------------------------------------------------ automatisations
def _automation_json(a: Automation) -> dict:
    return {"id": a.id, "name": a.name, "enabled": a.enabled, "definition": a.definition,
            "run_count": a.run_count, "last_run_at": _iso(a.last_run_at), "updated_at": _iso(a.updated_at)}


@router.get("/guilds/{guild_id}/automations")
async def list_automations(request: Request, guild_id: int):
    ctx = await auth.require_guild(request, guild_id)
    with session_scope() as db:
        rows = db.scalars(select(Automation).where(Automation.guild_id == guild_id).order_by(Automation.id)).all()
        return {"items": [_automation_json(a) for a in rows], "catalog": auto_mod.catalog(),
                "limits": premium_mod.limits(ctx.premium)}


def _validate_auto(ctx: auth.GuildContext, payload: dict) -> auto_mod.AutomationIn:
    try:
        return auto_mod.validate_automation(payload, max_actions=premium_mod.limits(ctx.premium)["automation_actions"])
    except (ValidationError, TemplateError, ValueError) as e:
        raise _err(e)


@router.post("/guilds/{guild_id}/automations")
async def create_automation(request: Request, guild_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _mutate_limit(ctx)
    item = _validate_auto(ctx, payload)
    with session_scope() as db:
        n = db.scalar(select(func.count()).select_from(Automation).where(Automation.guild_id == guild_id))
        if n >= premium_mod.limits(ctx.premium)["automations"]:
            raise HTTPException(402, {"message": "Limite d'automatisations de votre offre atteinte.", "errors": []})
        a = Automation(guild_id=guild_id, name=item.name, enabled=item.enabled,
                       definition=item.definition.model_dump(mode="json"), created_by=ctx.user.id)
        db.add(a)
        db.flush()
        _audit(db, ctx, "automation.create", {"id": a.id})
        return {"ok": True, "item": _automation_json(a)}


@router.put("/guilds/{guild_id}/automations/{auto_id}")
async def update_automation(request: Request, guild_id: int, auto_id: int, payload: dict = Body(...)):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    _mutate_limit(ctx)
    item = _validate_auto(ctx, payload)
    with session_scope() as db:
        a = db.get(Automation, auto_id)
        if a is None or a.guild_id != guild_id:
            raise HTTPException(404, "Automatisation introuvable")
        a.name, a.enabled, a.definition = item.name, item.enabled, item.definition.model_dump(mode="json")
        _audit(db, ctx, "automation.update", {"id": auto_id})
        return {"ok": True, "item": _automation_json(a)}


@router.delete("/guilds/{guild_id}/automations/{auto_id}")
async def delete_automation(request: Request, guild_id: int, auto_id: int):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    with session_scope() as db:
        a = db.get(Automation, auto_id)
        if a is None or a.guild_id != guild_id:
            raise HTTPException(404, "Automatisation introuvable")
        db.delete(a)
        _audit(db, ctx, "automation.delete", {"id": auto_id})
    return {"ok": True}


@router.post("/guilds/{guild_id}/automations/{auto_id}/test")
async def test_automation(request: Request, guild_id: int, auto_id: int):
    """Exécute l'automatisation à blanc : le bot l'évalue avec vous comme membre
    et rapporte ce qu'il AURAIT fait, sans sanction réelle."""
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    ratelimit.hit(f"job:{guild_id}", 10, 60)
    with session_scope() as db:
        a = db.get(Automation, auto_id)
        if a is None or a.guild_id != guild_id:
            raise HTTPException(404, "Automatisation introuvable")
        try:
            job = jobs.enqueue(db, guild_id, "test_automation", {"automation_id": auto_id,
                                                                  "member_id": str(ctx.user.id)}, ctx.user.id)
        except jobs.JobError as e:
            raise HTTPException(429, str(e))
        return {"ok": True, "job_id": job.id}


# ------------------------------------------------------------------ Premium & abonnement
@router.get("/guilds/{guild_id}/premium")
async def premium_status(request: Request, guild_id: int):
    ctx = await auth.require_guild(request, guild_id)
    s = get_settings()
    with session_scope() as db:
        sub = premium_mod.get_subscription(db, guild_id)
        data = None
        if sub:
            data = {"status": sub.status, "source": sub.source, "plan": sub.plan,
                    "current_period_end": _iso(sub.current_period_end),
                    "cancel_at_period_end": sub.cancel_at_period_end, "has_portal": bool(sub.stripe_customer_id)}
    return {"premium": ctx.premium, "subscription": data, "features": premium_mod.FEATURES,
            "limits": {"free": premium_mod.LIMITS["free"], "premium": premium_mod.LIMITS["premium"]},
            "stripe_enabled": s.stripe_enabled, "dev_premium": s.premium_dev_mode and is_owner(ctx.user.id)}


@router.post("/guilds/{guild_id}/premium/dev")
async def dev_premium(request: Request, guild_id: int, payload: dict = Body(...)):
    """Premium de test : propriétaire de Nexbot uniquement, et jamais en production."""
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    if not is_owner(ctx.user.id) or not get_settings().premium_dev_mode:
        raise HTTPException(404, "Introuvable")
    with session_scope() as db:
        if payload.get("action") == "revoke":
            premium_mod.revoke_dev_premium(db, guild_id)
        else:
            premium_mod.grant_dev_premium(db, guild_id, ctx.user.id, int(payload.get("days", 30)))
        _audit(db, ctx, "premium.dev", {"action": payload.get("action", "grant")})
    return {"ok": True}


@router.post("/guilds/{guild_id}/premium/checkout")
async def checkout(request: Request, guild_id: int):
    ctx = await auth.require_guild(request, guild_id, mutate=True)
    ratelimit.hit(f"checkout:{ctx.user.id}", 5, 60)
    try:
        return {"url": billing.create_checkout(guild_id, ctx.user.id)}
    except billing.BillingDisabled as e:
        raise HTTPException(503, str(e))


@router.post("/guilds/{guild_id}/premium/portal")
async def portal(request: Request, guild_id: int):
    await auth.require_guild(request, guild_id, mutate=True)
    with session_scope() as db:
        sub = premium_mod.get_subscription(db, guild_id)
        customer = sub.stripe_customer_id if sub else None
    if not customer:
        raise HTTPException(404, "Aucun abonnement Stripe pour ce serveur")
    try:
        return {"url": billing.create_portal(customer, guild_id)}
    except billing.BillingDisabled as e:
        raise HTTPException(503, str(e))


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> Any:
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = billing.verify_event(payload, signature)
    except billing.BillingDisabled:
        raise HTTPException(503, "Stripe non configuré")
    except Exception:
        raise HTTPException(400, "Signature invalide")
    with session_scope() as db:
        result = billing.handle_event(db, event)
    return {"received": True, "result": result}
