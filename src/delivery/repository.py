"""Accès lecture des destinataires actifs depuis la DB."""
from __future__ import annotations

from sqlalchemy import select

from ..database import get_session
from ..models import Recipient


def active_recipients(
    channel: str,
    *,
    frequencies: list[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Retourne la liste `(address, name)` des recipients actifs du canal.

    Args:
        channel: "email" | "whatsapp"
        frequencies: si fourni, filtre aux recipients dont `frequency` est
            dans la liste. Sinon, renvoie tous les recipients actifs (comportement
            legacy). Utilisé par les pipelines daily / weekly pour cibler les
            bonnes personnes selon le type de brief.

    Ouvre sa propre session — appelée tôt dans le pipeline, puis cachée en mémoire
    pendant la durée de l'envoi (les lists sont petites, < 10 entrées typiques).
    """
    with get_session() as s:
        stmt = (
            select(Recipient.address, Recipient.name)
            .where(Recipient.channel == channel)
            .where(Recipient.enabled.is_(True))
        )
        if frequencies:
            stmt = stmt.where(Recipient.frequency.in_(frequencies))
        rows = s.execute(stmt.order_by(Recipient.id)).all()
    return [(addr, name) for addr, name in rows]


# --- Logique de matching brief_type ↔ frequency -----------------------------

def frequencies_for_brief(
    brief_type: str,
    brief_payload: dict | None = None,
) -> list[str]:
    """Retourne les fréquences cibles d'un brief selon son type.

    Matrice de décision :
      - brief daily sans conviction forte   → ['daily']
      - brief daily avec conviction ≥ 4     → ['daily', 'critical_only']
      - brief weekly                        → ['daily', 'weekly', 'critical_only']
        (tout le monde reçoit le weekly — c'est un audit, son intérêt est universel)
      - brief type inconnu (futur)          → ['daily'] (safe default)
    """
    if brief_type == "weekly":
        return ["daily", "weekly", "critical_only"]
    if brief_type == "daily":
        if _has_critical_conviction(brief_payload):
            return ["daily", "critical_only"]
        return ["daily"]
    return ["daily"]


def _has_critical_conviction(payload: dict | None) -> bool:
    """True si une opportunité du brief a conviction ≥ 4 — déclencheur du
    mode 'critical_only'.
    """
    if not payload:
        return False
    for opp in payload.get("opportunities") or []:
        if not isinstance(opp, dict):
            continue
        conv = opp.get("conviction")
        if isinstance(conv, (int, float)) and conv >= 4:
            return True
    return False
