"""Rate limit simple pour les magic links — table-backed (pas de Redis).

Deux limites complémentaires :
- **Par email** : 5 demandes/heure. Protège un utilisateur légitime d'un
  abus (mais ne couvre pas le probing d'emails inconnus).
- **Par IP** : 20 demandes/heure. Empêche un attaquant d'énumérer les
  emails whitelistés en itérant sans limite — la limite email ne fire pas
  sur les emails inconnus (pas d'insertion → pas de compteur).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import LoginToken

MAX_REQUESTS_PER_HOUR = 5
MAX_IP_REQUESTS_PER_HOUR = 20


class RateLimitExceeded(Exception):
    pass


def requests_last_hour(session: Session, email: str) -> int:
    threshold = datetime.now(UTC) - timedelta(hours=1)
    return session.execute(
        select(func.count(LoginToken.id))
        .where(LoginToken.email == email)
        .where(LoginToken.created_at >= threshold)
    ).scalar_one()


def ip_requests_last_hour(session: Session, ip: str) -> int:
    threshold = datetime.now(UTC) - timedelta(hours=1)
    return session.execute(
        select(func.count(LoginToken.id))
        .where(LoginToken.requested_ip == ip)
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


def check_ip_rate_limit(session: Session, ip: str) -> None:
    """Lève `RateLimitExceeded` si l'IP a dépassé sa cadence globale.

    À appeler **avant** le lookup user, sinon un attaquant qui probe des
    emails inexistants n'est jamais compté (la limite email n'alimente son
    compteur que lorsqu'un `LoginToken` est réellement inséré).
    """
    if not ip or ip == "unknown":
        return
    count = ip_requests_last_hour(session, ip)
    if count >= MAX_IP_REQUESTS_PER_HOUR:
        raise RateLimitExceeded(
            f"Trop de demandes depuis {ip} "
            f"({count}/{MAX_IP_REQUESTS_PER_HOUR}). Réessaie dans une heure."
        )
