"""Lecture / écriture des paramètres de modules, avec application des règles Premium.

Principe : les valeurs Premium enregistrées sont TOUJOURS conservées en base,
même après l'expiration d'un abonnement. Lorsqu'un serveur n'est pas Premium,
`effective()` renvoie les valeurs par défaut pour ces options : le bot ne les
applique plus, et elles reviennent automatiquement après un réabonnement.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DashboardAudit, GuildSettings, utcnow
from .schemas import MODULES, ModuleModel, field_meta


class SettingsError(ValueError):
    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


class PremiumRequired(SettingsError):
    pass


def _row(session: Session, guild_id: int, module: str) -> GuildSettings | None:
    return session.get(GuildSettings, {"guild_id": guild_id, "module": module})


def stored(session: Session, guild_id: int, module: str) -> ModuleModel:
    """Valeurs enregistrées (y compris Premium), complétées par les valeurs par défaut."""
    spec = MODULES[module]
    row = _row(session, guild_id, module)
    data = dict(row.data) if row and row.data else {}
    try:
        return spec.model.model_validate(data)
    except ValidationError:
        # Données anciennes devenues invalides après une mise à jour : on garde ce qui reste valide.
        default = spec.model().model_dump()
        clean = {}
        for k, v in data.items():
            if k in default:
                try:
                    spec.model.model_validate({**default, **clean, k: v})
                    clean[k] = v
                except ValidationError:
                    pass
        return spec.model.model_validate({**default, **clean})


def apply_plan(model: ModuleModel, module: str, premium: bool) -> ModuleModel:
    """Valeurs réellement appliquées selon le plan du serveur."""
    if premium:
        return model
    spec = MODULES[module]
    defaults = spec.model()
    data = model.model_dump()
    for name in spec.model.model_fields:
        meta = field_meta(spec.model, name)
        if spec.premium_only or meta.get("premium"):
            data[name] = getattr(defaults, name).model_dump() if hasattr(getattr(defaults, name), "model_dump") \
                else getattr(defaults, name)
        elif "free_max" in meta:
            v = data[name]
            if isinstance(v, list):
                data[name] = v[: meta["free_max"]]
            elif isinstance(v, int):
                data[name] = min(v, meta["free_max"])
    return spec.model.model_validate(data)


def effective(session: Session, guild_id: int, module: str, premium: bool) -> ModuleModel:
    return apply_plan(stored(session, guild_id, module), module, premium)


def _dump(v: Any) -> Any:
    return v.model_dump() if hasattr(v, "model_dump") else v


def save(session: Session, guild_id: int, module: str, incoming: dict[str, Any], *, user_id: int,
         premium: bool) -> ModuleModel:
    """Valide et enregistre une mise à jour (partielle ou complète) d'un module."""
    if module not in MODULES:
        raise SettingsError("Module inconnu")
    spec = MODULES[module]
    if not isinstance(incoming, dict):
        raise SettingsError("Format invalide")
    if spec.premium_only and not premium:
        raise PremiumRequired(f"Le module « {spec.title} » est réservé au Premium.")

    current = stored(session, guild_id, module)
    merged = {**current.model_dump(), **{k: v for k, v in incoming.items() if k in spec.model.model_fields}}
    try:
        new = spec.model.model_validate(merged)
    except (ValidationError, ValueError) as exc:
        errors = []
        if isinstance(exc, ValidationError):
            for e in exc.errors():
                errors.append({"field": ".".join(str(p) for p in e["loc"]),
                               "message": str(e["msg"]).removeprefix("Value error, ")})
        else:
            errors.append({"field": "", "message": str(exc)})
        raise SettingsError(errors[0]["message"] if errors else "Paramètres invalides", errors) from exc

    if not premium:
        locked = []
        for name in spec.model.model_fields:
            meta = field_meta(spec.model, name)
            old_v, new_v = _dump(getattr(current, name)), _dump(getattr(new, name))
            if meta.get("premium") and old_v != new_v:
                locked.append(meta.get("label", name))
            elif "free_max" in meta:
                limit = meta["free_max"]
                size = len(new_v) if isinstance(new_v, list) else new_v
                old_size = len(old_v) if isinstance(old_v, list) else old_v
                if isinstance(size, int) and size > limit and size != old_size:
                    locked.append(f"{meta.get('label', name)} (limite gratuite : {limit})")
        if locked:
            raise PremiumRequired("Option(s) Premium : " + ", ".join(locked),
                                  [{"field": "", "message": f"Premium requis : {x}"} for x in locked])

    row = _row(session, guild_id, module)
    if row is None:
        row = GuildSettings(guild_id=guild_id, module=module)
        session.add(row)
    row.data = new.model_dump(mode="json")
    row.updated_by = user_id
    row.updated_at = utcnow()
    changed = sorted(k for k in spec.model.model_fields
                     if _dump(getattr(current, k)) != _dump(getattr(new, k)))
    session.add(DashboardAudit(guild_id=guild_id, user_id=user_id, action=f"settings.{module}",
                               details={"changed": changed}))
    return new


def last_change(session: Session) -> Any:
    """Horodatage de la dernière modification (le bot s'en sert pour invalider son cache)."""
    return session.scalar(select(GuildSettings.updated_at).order_by(GuildSettings.updated_at.desc()).limit(1))
