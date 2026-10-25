"""Moteur de base de données partagé (SQLite en local, PostgreSQL en production)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import BigInteger, Integer, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

# Identifiant auto-incrémenté compatible SQLite (INTEGER) et PostgreSQL (BIGINT).
IdType = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # Serverless (Vercel) : petites connexions, recyclées souvent.
        kwargs.update(pool_size=3, max_overflow=2, pool_recycle=300)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # bot + Dashboard en parallèle
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    _engine = _make_engine(url or get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction courte : commit si tout va bien, rollback sinon."""
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
