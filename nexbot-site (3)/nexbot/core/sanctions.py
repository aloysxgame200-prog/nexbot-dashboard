"""Opérations sur les sanctions partagées par le bot (/reason) et le Dashboard (page Sanctions)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Sanction

REASON_MAX = 1000


class ReasonError(ValueError):
    """Raison refusée (vide ou trop longue)."""


def clean_reason(reason: str | None) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ReasonError("La nouvelle raison ne peut pas être vide.")
    if len(reason) > REASON_MAX:
        raise ReasonError(f"La raison est limitée à {REASON_MAX} caractères.")
    return reason


def update_reason(db: Session, guild_id: int, case_id: int, reason: str | None) -> tuple[str, int | None, str]:
    """Remplace la raison d'une sanction de CE serveur.

    Renvoie (statut, ID du membre sanctionné, ancienne raison) avec statut :
    « ok », « same » (raison identique, rien n'est modifié) ou « missing » (introuvable sur ce serveur).
    Lève ReasonError si la nouvelle raison est vide ou trop longue.
    """
    reason = clean_reason(reason)
    s = db.get(Sanction, case_id)
    if s is None or s.guild_id != guild_id:
        return "missing", None, ""
    old = s.reason or ""
    if old == reason:
        return "same", s.user_id, old
    s.reason = reason
    return "ok", s.user_id, old
