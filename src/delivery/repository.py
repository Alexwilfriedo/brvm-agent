"""Accès lecture des destinataires actifs depuis la DB."""
from __future__ import annotations

from sqlalchemy import select

from ..database import get_session
from ..models import Recipient


def active_recipients(channel: str) -> list[tuple[str, str | None]]:
    """Retourne la liste `(address, name)` des recipients actifs du canal.

    Ouvre sa propre session — appelle tôt dans le pipeline, puis cache en mémoire
    pendant la durée de l'envoi (les lists sont petites, < 10 entrées typiques).
    """
    with get_session() as s:
        rows = s.execute(
            select(Recipient.address, Recipient.name)
            .where(Recipient.channel == channel)
            .where(Recipient.enabled.is_(True))
            .order_by(Recipient.id)
        ).all()
    return [(addr, name) for addr, name in rows]
