"""Endpoints de consultation : briefs et signaux historiques."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..database import get_session
from ..models import Brief
from .deps import require_admin

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

    class Config:
        from_attributes = True


@router.get("", response_model=list[BriefSummaryOut])
def list_briefs(limit: int = Query(30, ge=1, le=200)):
    with get_session() as s:
        briefs = s.execute(
            select(Brief).order_by(Brief.brief_date.desc()).limit(limit)
        ).scalars().all()
        return [
            BriefSummaryOut(
                id=b.id,
                brief_date=b.brief_date,
                summary_markdown=b.summary_markdown,
                email_sent=b.email_sent,
                whatsapp_sent=b.whatsapp_sent,
                delivery_status=b.delivery_status,
                signals_count=len(b.signals),
            )
            for b in briefs
        ]


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
        )
