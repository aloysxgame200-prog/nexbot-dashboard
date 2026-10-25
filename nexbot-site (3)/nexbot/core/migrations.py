"""Migrations versionnées, idempotentes, compatibles SQLite et PostgreSQL.

Pour faire évoluer le schéma : ajouter une fonction `_vN(conn)` à MIGRATIONS.
Elle n'est exécutée qu'une seule fois par base. Les données existantes
(paramètres, personnalisations Premium…) sont toujours conservées.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, Engine

from . import models  # noqa: F401  (enregistre les tables)
from .db import Base, get_engine

log = logging.getLogger("nexbot.migrations")


def _v1(conn: Connection) -> None:
    """Schéma initial."""
    Base.metadata.create_all(conn)


def _v2(conn: Connection) -> None:
    """Statistiques d'utilisation du bot et blacklist globale (tables ajoutées, rien n'est modifié)."""
    for model in (models.ActiveUser, models.CommandUsage, models.GlobalBlacklist, models.GuildEvent):
        model.__table__.create(conn, checkfirst=True)


MIGRATIONS: list[tuple[int, Callable[[Connection], None]]] = [
    (1, _v1),
    (2, _v2),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(conn: Connection) -> int:
    if not inspect(conn).has_table("schema_version"):
        return 0
    row = conn.execute(select(models.SchemaVersion.version).order_by(models.SchemaVersion.version.desc())).first()
    return row[0] if row else 0


def run_migrations(engine: Engine | None = None) -> int:
    engine = engine or get_engine()
    with engine.begin() as conn:
        version = current_version(conn)
        for number, migrate in MIGRATIONS:
            if number > version:
                log.info("Migration v%s…", number)
                migrate(conn)
                conn.execute(models.SchemaVersion.__table__.insert().values(version=number))
                version = number
    return version


if __name__ == "__main__":  # python -m nexbot.core.migrations
    logging.basicConfig(level=logging.INFO)
    print(f"Base de données Nexbot à jour (schéma v{run_migrations()}).")
