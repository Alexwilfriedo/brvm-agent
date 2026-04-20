"""Rate limit simple pour les magic links — table-backed (pas de Redis).

Règle : 5 demandes de magic link par email par heure.
On compte les `LoginToken` créés dans les 60 dernières minutes pour un email.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import LoginToken

MAX_REQUESTS_PER_HOUR = 5


def requests_last_hour(session: Session, email: str) -> int:
    threshold = datetime.now(UTC) - timedelta(hours=1)
    return session.execute(
        select(func.count(LoginToken.id))
        .where(LoginToken.email == email)
        .where(LoginToken.created_at >= threshold)
    ).scalar_one()


def check_rate_limit(session: Session, email: str) -> None:
    """Lève `RateLimitExceeded` si l'email a dépassé sa cadence."""
    count = requests_last_hour(session, email)
    if count >= MAX_REQUESTS_PER_HOUR:
        raise RateLimitExceeded(
            f"Trop de demandes de lien pour {email} dans la dernière heure "
            f"({count}/{MAX_REQUESTS_PER_HOUR}). Réessaie plus tard."
        )


class RateLimitExceeded(Exception):
    pass
