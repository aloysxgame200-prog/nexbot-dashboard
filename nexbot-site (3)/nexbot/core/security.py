"""Primitives de sécurité partagées par le bot et le Dashboard.

L'identifiant du propriétaire est une constante du code, et non une variable
d'environnement : il ne peut pas être modifié par une configuration, une
requête du Dashboard, une commande slash ou une API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

OWNER_ID: int = 598080343958093831


def is_owner(user_id: object) -> bool:
    """Seule autorité pour les commandes globales `*`.

    Comparaison stricte sur l'identifiant numérique : jamais sur un pseudo,
    un nom, un rôle ou une permission de serveur.
    """
    if isinstance(user_id, bool):
        return False
    if isinstance(user_id, int):
        return user_id == OWNER_ID
    if isinstance(user_id, str) and user_id.isdigit():
        return int(user_id) == OWNER_ID
    return False


# --- Permissions Discord (bits) ------------------------------------------------
PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20


def can_manage_guild(permissions: int | str, is_guild_owner: bool = False) -> bool:
    """Un utilisateur peut configurer un serveur s'il en est propriétaire,
    administrateur ou s'il possède « Gérer le serveur »."""
    if is_guild_owner:
        return True
    perms = int(permissions)
    return bool(perms & PERM_ADMINISTRATOR or perms & PERM_MANAGE_GUILD)


# --- Chiffrement des tokens OAuth stockés -------------------------------------
def _fernet(secret_key: str) -> Fernet:
    key = hashlib.sha256(("nexbot-token-encryption:" + secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret_key: str, value: str) -> str:
    return _fernet(secret_key).encrypt(value.encode()).decode()


def decrypt(secret_key: str, value: str) -> str | None:
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# --- Jetons de session -----------------------------------------------------------
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """On ne stocke que l'empreinte du jeton de session en base."""
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
