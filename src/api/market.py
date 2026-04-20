"""Endpoints marché : snapshot agrégé + analyse Sonnet cachée."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ..analysis.market import build_snapshot, build_ticker_detail, generate_analysis
from ..database import get_session
from .deps import require_admin

router = APIRouter(prefix="/api/market", tags=["market"], dependencies=[Depends(require_admin)])


class AnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    trading_date: datetime
    narrative_fr: str
    key_stats: dict
    model_used: str | None
    input_tokens: int
    output_tokens: int
    generated_at: datetime


@router.get("/snapshot")
def get_snapshot(
    date: str | None = Query(None, description="Date ISO (YYYY-MM-DD), défaut = dernière séance"),
):
    """Retourne le snapshot agrégé du marché : top movers, secteurs, heatmap."""
    trading_date = None
    if date:
        try:
            trading_date = datetime.fromisoformat(date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Format date invalide (YYYY-MM-DD)") from e
    with get_session() as s:
        snap = build_snapshot(s, trading_date)
        if snap.get("quotes_count", 0) == 0:
            raise HTTPException(status_code=404, detail="Aucune cotation pour cette date")
        return snap


@router.get("/analysis", response_model=AnalysisOut)
def get_analysis(
    date: str | None = Query(None),
    force: bool = Query(False, description="Force la régénération via Sonnet"),
):
    """Retourne l'analyse du jour (générée par Sonnet, cachée en DB).

    Cache-first : si l'analyse existe déjà pour la date demandée, on la renvoie.
    `force=true` régénère (utile si les données ont changé ou pour un nouveau ton).
    """
    trading_date = None
    if date:
        try:
            trading_date = datetime.fromisoformat(date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Format date invalide") from e
    with get_session() as s:
        analysis = generate_analysis(s, trading_date, force=force)
        if analysis is None:
            raise HTTPException(
                status_code=404,
                detail="Impossible de générer une analyse : pas de données ou Sonnet KO",
            )
        return AnalysisOut.model_validate(analysis)


@router.get("/tickers/{ticker}")
def get_ticker_detail(
    ticker: str,
    days: int = Query(90, ge=1, le=365, description="Fenêtre historique en jours"),
    news_limit: int = Query(10, ge=0, le=50),
):
    """Fiche détaillée d'un ticker : dernière cotation + série + stats + news.

    Répond 404 si le ticker n'existe ni dans le référentiel BRVM ni en DB.
    """
    with get_session() as s:
        detail = build_ticker_detail(s, ticker, days=days, news_limit=news_limit)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Ticker inconnu : {ticker}")
        return detail


@router.post("/analysis/regenerate", response_model=AnalysisOut)
def regenerate_analysis(date: str | None = Query(None)):
    """Force la régénération de l'analyse (équivalent `GET /analysis?force=true`)."""
    trading_date = None
    if date:
        try:
            trading_date = datetime.fromisoformat(date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Format date invalide") from e
    with get_session() as s:
        analysis = generate_analysis(s, trading_date, force=True)
        if analysis is None:
            raise HTTPException(
                status_code=404,
                detail="Impossible de générer : pas de données ou Sonnet KO",
            )
        return AnalysisOut.model_validate(analysis)
