"""Endpoints de consultation : briefs et signaux historiques."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, select

from ..database import get_session
from ..models import Brief
from ..pipeline import RedeliveryError, redeliver_brief
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, PaginatedResponse, ilike_any, paginate

router = APIRouter(prefix="/api/briefs", tags=["briefs"], dependencies=[Depends(require_admin)])


class SignalOut(BaseModel):
    ticker: str
    direction: str
    conviction: int
    thesis: str
    price_at_signal: float | None

    class Config:
        from_attributes = True


class BriefSummaryOut(BaseModel):
    id: int
    brief_date: datetime
    summary_markdown: str
    email_sent: bool
    whatsapp_sent: bool
    delivery_status: str
    signals_count: int
    revision: int = 1
    revised_at: datetime | None = None

    class Config:
        from_attributes = True


class BriefDetailOut(BaseModel):
    id: int
    brief_date: datetime
    summary_markdown: str
    payload: dict
    signals: list[SignalOut]
    email_sent: bool
    whatsapp_sent: bool
    delivery_status: str
    delivery_errors: str | None
    revision: int = 1
    revised_at: datetime | None = None

    class Config:
        from_attributes = True


@router.get("", response_model=PaginatedResponse[BriefSummaryOut])
def list_briefs(
    q: str | None = Query(None, description="Recherche fuzzy dans summary"),
    delivery_status: str | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    from sqlalchemy.orm import selectinload
    with get_session() as s:
        stmt = (
            select(Brief)
            .options(selectinload(Brief.signals))  # évite N+1 sur signals_count
            .order_by(Brief.brief_date.desc())
        )
        if delivery_status:
            stmt = stmt.where(Brief.delivery_status == delivery_status)
        if q:
            stmt = stmt.where(ilike_any([cast(Brief.summary_markdown, String)], q))
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[BriefSummaryOut](
            items=[
                BriefSummaryOut(
                    id=b.id,
                    brief_date=b.brief_date,
                    summary_markdown=b.summary_markdown,
                    email_sent=b.email_sent,
                    whatsapp_sent=b.whatsapp_sent,
                    delivery_status=b.delivery_status,
                    signals_count=len(b.signals),
                    revision=b.revision,
                    revised_at=b.revised_at,
                )
                for b in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/today")
def get_today_brief():
    """Brief du jour (date calendaire locale UTC) ou `null`.

    Utilisé par le dashboard pour afficher le "signal fort du jour" sans
    parser la liste paginée côté front. Renvoie `null` si aucun brief
    n'existe pour aujourd'hui.
    """
    from datetime import UTC, datetime, timedelta
    from sqlalchemy.orm import selectinload
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    with get_session() as s:
        brief = s.execute(
            select(Brief)
            .options(selectinload(Brief.signals))
            .where(Brief.brief_date >= day_start)
            .where(Brief.brief_date < day_end)
            .order_by(Brief.revision.desc())
            .limit(1)
        ).scalar_one_or_none()
        if not brief:
            return None
        return BriefDetailOut(
            id=brief.id,
            brief_date=brief.brief_date,
            summary_markdown=brief.summary_markdown,
            payload=brief.payload,
            signals=[SignalOut.model_validate(sig) for sig in brief.signals],
            email_sent=brief.email_sent,
            whatsapp_sent=brief.whatsapp_sent,
            delivery_status=brief.delivery_status,
            delivery_errors=brief.delivery_errors,
            revision=brief.revision,
            revised_at=brief.revised_at,
        )


@router.get("/{brief_id}", response_model=BriefDetailOut)
def get_brief(brief_id: int):
    with get_session() as s:
        brief = s.get(Brief, brief_id)
        if not brief:
            raise HTTPException(status_code=404, detail="Brief introuvable")
        return BriefDetailOut(
            id=brief.id,
            brief_date=brief.brief_date,
            summary_markdown=brief.summary_markdown,
            payload=brief.payload,
            signals=[SignalOut.model_validate(sig) for sig in brief.signals],
            email_sent=brief.email_sent,
            whatsapp_sent=brief.whatsapp_sent,
            delivery_status=brief.delivery_status,
            delivery_errors=brief.delivery_errors,
            revision=brief.revision,
            revised_at=brief.revised_at,
        )


class RedeliverOut(BaseModel):
    brief_id: int
    status: str
    email_ok: bool
    whatsapp_ok: bool
    errors: list[str]


@router.post("/{brief_id}/redeliver", response_model=RedeliverOut)
def redeliver(brief_id: int) -> RedeliverOut:
    """Rejoue email + WhatsApp pour un brief déjà synthétisé.

    Typiquement utilisé depuis la vue Run quand l'envoi initial a échoué
    (ex : timeout SMTP Brevo). Ne relance pas Opus, n'incrémente pas la
    révision, ne crée pas de nouveau `pipeline_runs`. Met à jour
    `briefs.delivery_status` / `delivery_errors` / `email_sent` /
    `whatsapp_sent`.

    Attention : peut bloquer jusqu'à ~30s si SMTP Brevo est injoignable —
    le client doit afficher un loader.
    """
    try:
        result = redeliver_brief(brief_id)
    except RedeliveryError as exc:
        # 404 si le brief n'existe pas, 409 sinon (état incompatible).
        msg = str(exc)
        code = 404 if "introuvable" in msg else 409
        raise HTTPException(status_code=code, detail=msg) from exc
    return RedeliverOut(brief_id=brief_id, **result)
