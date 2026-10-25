"""Limitation des requêtes sensibles (fenêtre glissante en mémoire).

En serverless (Vercel), chaque instance a sa propre mémoire : la limite reste
efficace contre les rafales mais n'est pas globale. Pour une limite globale,
brancher un Redis (Upstash, offre gratuite) dans `hit()`.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException

_hits: dict[str, deque] = defaultdict(deque)


def hit(key: str, limit: int, window: float) -> None:
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Trop de requêtes, patientez quelques secondes.")
    q.append(now)
    if len(_hits) > 50_000:
        _hits.clear()


def reset() -> None:
    _hits.clear()
