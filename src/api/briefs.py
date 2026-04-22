"""Endpoints de consultation : briefs et signaux historiques."""
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
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
    brief_type: str = "daily"
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
    brief_type: str = "daily"
    summary_markdown: str
    payload: dict
    signals: list[SignalOut]
    email_sent: bool
    whatsapp_sent: bool
    delivery_status: str
    delivery_errors: str | None
    revision: int = 1
    revised_at: datetime | None = None
    # Q-1 A/B : payload produit par le modèle alternatif (Sonnet si principal Opus).
    # Null quand le test A/B est désactivé ou que l'appel alt a échoué.
    payload_alt: dict | None = None
    model_alt: str | None = None

    class Config:
        from_attributes = True


@router.get("", response_model=PaginatedResponse[BriefSummaryOut])
def list_briefs(
    q: str | None = Query(None, description="Recherche fuzzy dans summary"),
    delivery_status: str | None = Query(None),
    brief_type: str | None = Query(
        None, description="Filtre par type : 'daily' ou 'weekly'. Omis = tous.",
    ),
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
        if brief_type:
            stmt = stmt.where(Brief.brief_type == brief_type)
        if q:
            stmt = stmt.where(ilike_any([cast(Brief.summary_markdown, String)], q))
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[BriefSummaryOut](
            items=[
                BriefSummaryOut(
                    id=b.id,
                    brief_date=b.brief_date,
                    brief_type=b.brief_type,
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
            brief_type=brief.brief_type,
            summary_markdown=brief.summary_markdown,
            payload=brief.payload,
            signals=[SignalOut.model_validate(sig) for sig in brief.signals],
            email_sent=brief.email_sent,
            whatsapp_sent=brief.whatsapp_sent,
            delivery_status=brief.delivery_status,
            delivery_errors=brief.delivery_errors,
            revision=brief.revision,
            revised_at=brief.revised_at,
            payload_alt=brief.payload_alt,
            model_alt=brief.model_alt,
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
            brief_type=brief.brief_type,
            summary_markdown=brief.summary_markdown,
            payload=brief.payload,
            signals=[SignalOut.model_validate(sig) for sig in brief.signals],
            email_sent=brief.email_sent,
            whatsapp_sent=brief.whatsapp_sent,
            delivery_status=brief.delivery_status,
            delivery_errors=brief.delivery_errors,
            revision=brief.revision,
            revised_at=brief.revised_at,
            payload_alt=brief.payload_alt,
            model_alt=brief.model_alt,
        )


class RedeliverOut(BaseModel):
    brief_id: int
    status: str
    email_ok: bool
    whatsapp_ok: bool
    errors: list[str]
    sent_to: list[str] = Field(default_factory=list)


class RecipientOverride(BaseModel):
    """Destinataire ciblé pour une re-livraison ad-hoc."""
    email: EmailStr
    name: str | None = Field(default=None, max_length=120)


class RedeliverIn(BaseModel):
    """Body optionnel de `POST /api/briefs/{id}/redeliver`.

    Si `recipients` est fourni et non vide, on envoie uniquement à cette
    liste (mode "ciblage ad-hoc" : WhatsApp skipé, delivery_status du brief
    non modifié). Sinon comportement standard : tous les destinataires actifs
    en DB + WhatsApp si activé.
    """
    recipients: list[RecipientOverride] | None = Field(
        default=None,
        max_length=50,
        description="Max 50 destinataires ad-hoc (anti-abus).",
    )


@router.post("/{brief_id}/redeliver", response_model=RedeliverOut)
def redeliver(
    brief_id: int,
    body: RedeliverIn | None = Body(default=None),
) -> RedeliverOut:
    """Rejoue email + WhatsApp pour un brief déjà synthétisé.

    Typiquement utilisé depuis la vue Run quand l'envoi initial a échoué
    (ex : timeout SMTP Brevo). Ne relance pas Opus, n'incrémente pas la
    révision, ne crée pas de nouveau `pipeline_runs`. Met à jour
    `briefs.delivery_status` / `delivery_errors` / `email_sent` /
    `whatsapp_sent`.

    Body optionnel `{recipients: [{email, name?}, ...]}` pour cibler des
    destinataires spécifiques (ex : renvoyer à une seule personne ou à une
    adresse ad-hoc). Dans ce mode, WhatsApp est skipé et le statut officiel
    du brief n'est pas modifié.

    Attention : peut bloquer jusqu'à ~30s si SMTP Brevo est injoignable —
    le client doit afficher un loader.
    """
    override: list[tuple[str, str | None]] | None = None
    if body and body.recipients:
        # Dédup case-insensitive sur l'email tout en préservant l'ordre.
        seen: set[str] = set()
        deduped: list[tuple[str, str | None]] = []
        for r in body.recipients:
            key = r.email.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append((r.email, r.name))
        if not deduped:
            raise HTTPException(status_code=400, detail="Liste de destinataires vide après déduplication.")
        override = deduped

    try:
        result = redeliver_brief(brief_id, email_recipients_override=override)
    except RedeliveryError as exc:
        # 404 si le brief n'existe pas, 409 sinon (état incompatible).
        msg = str(exc)
        code = 404 if "introuvable" in msg else 409
        raise HTTPException(status_code=code, detail=msg) from exc
    return RedeliverOut(brief_id=brief_id, **result)
