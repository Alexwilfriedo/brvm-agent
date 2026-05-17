"""Endpoints d'analyse d'investissement on-demand par Opus.

Répond à la question : **faut-il investir sur ce ticker et pourquoi ?** La
logique métier vit dans `src/analysis/investment_advisor.py` ; ce module ne
fait que la plomberie HTTP (validation, dédup cache, persistance).

Protection admin (X-Admin-Token ou JWT magic link) : endpoint coûteux (appel
Opus), strictement personnel.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from ..analysis.investment_advisor import InvestmentAdvisor
from ..database import get_session
from ..models import InvestmentAnalysis, Quote
from .auth import UserOut, current_user
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, MAX_LIMIT, PaginatedResponse, paginate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/investment-analyses",
    tags=["investment-analyses"],
    dependencies=[Depends(require_admin)],
)

Horizon = Literal["short", "medium", "long"]

# Dédup cache : si une analyse pour `(ticker, horizon)` existe depuis moins que
# ce délai, on la renvoie telle quelle au lieu de rappeler Opus. Protège le
# budget LLM contre les double-clicks / boucles UI. Réglable au besoin.
_CACHE_TTL_MINUTES = 15


# --- Schemas ----------------------------------------------------------------


class InvestmentAnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    horizon: str
    recommendation: str
    confidence: float
    price_at_analysis: float
    price_target: float | None
    stop_loss: float | None
    time_horizon_days: int | None
    payload: dict
    input_tokens: int
    output_tokens: int
    model_used: str | None
    requested_by: str | None
    requested_at: datetime
    from_cache: bool


class InvestmentAnalysisSummaryOut(BaseModel):
    """Vue légère pour le listing — exclut le payload complet."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    horizon: str
    recommendation: str
    confidence: float
    price_at_analysis: float
    price_target: float | None
    requested_at: datetime
    from_cache: bool


class InvestmentAnalysisCreate(BaseModel):
    ticker: str = Field(..., min_length=2, max_length=16)
    horizon: Horizon

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, v: str) -> str:
        return v.strip().upper()


# --- Routes -----------------------------------------------------------------


@router.post(
    "",
    response_model=InvestmentAnalysisOut,
    status_code=status.HTTP_201_CREATED,
)
def create_analysis(
    payload: InvestmentAnalysisCreate,
    user: Annotated[UserOut, Depends(current_user)],
) -> InvestmentAnalysisOut:
    """Produit une recommandation d'investissement pour un ticker + horizon.

    Flux :
    1. Valide que le ticker existe dans `quotes` (404 sinon — évite de
       gaspiller des tokens Opus).
    2. Vérifie le cache dédup (15 min par défaut) — renvoie la dernière
       analyse si fraîche, flag `from_cache=true`.
    3. Sinon appelle `InvestmentAdvisor.analyze()` (5-20s — le client doit
       afficher un loader).
    4. Persiste + renvoie.

    Status codes :
      - 201 : analyse produite (cache miss) ou re-servie (cache hit — même
              code pour simplicité ; `from_cache` dans le body lève l'ambiguïté).
      - 404 : ticker inconnu en DB.
      - 401/403 : auth.
    """
    ticker = payload.ticker
    horizon = payload.horizon

    with get_session() as s:
        # 1. Validation ticker AVANT tout appel LLM
        if not _ticker_exists(ticker, s):
            raise HTTPException(
                status_code=404,
                detail=f"Ticker {ticker!r} inconnu (aucune quote en base).",
            )

        # 2. Dédup cache
        cached = _find_recent_analysis(ticker, horizon, s)
        if cached is not None:
            logger.info(
                f"InvestmentAnalysis cache hit: ticker={ticker} horizon={horizon} "
                f"id={cached.id} age_minutes={_age_minutes(cached.requested_at):.1f}",
            )
            # Renvoie une copie marquée from_cache=True sans recréer de ligne DB
            # (on réutilise l'entrée existante pour préserver l'intégrité du
            # backtest). Le flag from_cache reflète "a déjà été renvoyé".
            return _to_out(cached, from_cache_override=True)

        # 3. Appel LLM
        advisor = InvestmentAdvisor()
        result = advisor.analyze(ticker=ticker, horizon=horizon, session=s)

        # 4. Persistance
        requested_by = user.email if user else None
        row = InvestmentAnalysis(
            ticker=ticker,
            horizon=horizon,
            recommendation=result.recommendation,
            confidence=result.confidence,
            price_at_analysis=result.price_at_analysis,
            price_target=result.price_target,
            stop_loss=result.stop_loss,
            time_horizon_days=result.time_horizon_days,
            payload=result.payload,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_used=result.model_used,
            requested_by=requested_by,
            from_cache=False,
        )
        s.add(row)
        s.flush()  # force l'attribution de `id` avant la sérialisation
        return _to_out(row)


@router.get("", response_model=PaginatedResponse[InvestmentAnalysisSummaryOut])
def list_analyses(
    ticker: str | None = Query(None, description="Filtre exact sur le ticker."),
    horizon: Horizon | None = Query(None),
    recommendation: Literal["buy", "hold", "avoid"] | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """Liste paginée des analyses passées — triées par plus récent d'abord."""
    with get_session() as s:
        stmt = select(InvestmentAnalysis).order_by(
            InvestmentAnalysis.requested_at.desc(),
        )
        if ticker:
            stmt = stmt.where(InvestmentAnalysis.ticker == ticker.strip().upper())
        if horizon:
            stmt = stmt.where(InvestmentAnalysis.horizon == horizon)
        if recommendation:
            stmt = stmt.where(InvestmentAnalysis.recommendation == recommendation)

        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[InvestmentAnalysisSummaryOut](
            items=[
                InvestmentAnalysisSummaryOut.model_validate(it) for it in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/{analysis_id}", response_model=InvestmentAnalysisOut)
def get_analysis(analysis_id: int):
    with get_session() as s:
        row = s.get(InvestmentAnalysis, analysis_id)
        if not row:
            raise HTTPException(
                status_code=404,
                detail="Analyse introuvable.",
            )
        return _to_out(row)


# --- Helpers ----------------------------------------------------------------


def _ticker_exists(ticker: str, session) -> bool:
    """True si au moins une quote existe pour ce ticker."""
    exists = session.execute(
        select(Quote.id).where(Quote.ticker == ticker).limit(1)
    ).scalar_one_or_none()
    return exists is not None


def _find_recent_analysis(
    ticker: str,
    horizon: str,
    session,
) -> InvestmentAnalysis | None:
    """Retourne la dernière analyse (ticker, horizon) si < _CACHE_TTL_MINUTES."""
    cutoff = datetime.now(UTC) - timedelta(minutes=_CACHE_TTL_MINUTES)
    return session.execute(
        select(InvestmentAnalysis)
        .where(InvestmentAnalysis.ticker == ticker)
        .where(InvestmentAnalysis.horizon == horizon)
        .where(InvestmentAnalysis.requested_at >= cutoff)
        .order_by(InvestmentAnalysis.requested_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _age_minutes(dt: datetime) -> float:
    """Minutes écoulées depuis dt (tz-aware)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() / 60


def _to_out(
    row: InvestmentAnalysis,
    *,
    from_cache_override: bool | None = None,
) -> InvestmentAnalysisOut:
    """Sérialise une ligne DB avec override possible du flag from_cache."""
    return InvestmentAnalysisOut(
        id=row.id,
        ticker=row.ticker,
        horizon=row.horizon,
        recommendation=row.recommendation,
        confidence=row.confidence,
        price_at_analysis=row.price_at_analysis,
        price_target=row.price_target,
        stop_loss=row.stop_loss,
        time_horizon_days=row.time_horizon_days,
        payload=row.payload,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        model_used=row.model_used,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        from_cache=from_cache_override if from_cache_override is not None else row.from_cache,
    )
