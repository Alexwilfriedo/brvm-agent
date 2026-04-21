"""Journal des trades exécutés par l'utilisateur (epic M-1).

Objectif : fermer la boucle de mesure du projet. Sans ce registre, impossible
de comparer les signaux `brvm-agent` aux décisions réellement prises et au
PnL réel. Voir `tools/backtest_signals.py` pour l'exploitation.

Protection admin (X-Admin-Token) : le registre est personnel.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from ..database import get_session
from ..models import Brief, Signal, Trade
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, MAX_LIMIT, PaginatedResponse, paginate

router = APIRouter(
    prefix="/api/trades",
    tags=["trades"],
    dependencies=[Depends(require_admin)],
)

Action = Literal["buy", "sell"]
Reason = Literal["brief", "intuition", "news", "other"]


# --- Schemas ----------------------------------------------------------------

class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    action: str
    quantity: int
    unit_price: float
    executed_at: datetime
    reason: str
    brief_id: int | None
    signal_id: int | None
    notes: str | None
    created_at: datetime


class TradeCreate(BaseModel):
    ticker: str = Field(..., min_length=2, max_length=16)
    action: Action
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., gt=0)
    executed_at: datetime | None = None
    reason: Reason = "other"
    brief_id: int | None = None
    signal_id: int | None = None
    notes: str | None = None

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, v: str) -> str:
        return v.strip().upper()


class TradePatch(BaseModel):
    reason: Reason | None = None
    brief_id: int | None = None
    signal_id: int | None = None
    notes: str | None = None


# --- Routes -----------------------------------------------------------------

@router.post("", response_model=TradeOut, status_code=status.HTTP_201_CREATED)
def create_trade(payload: TradeCreate):
    """Enregistre un trade exécuté. Utilisation depuis CLI :

        curl -X POST https://<host>/api/trades \\
          -H "X-Admin-Token: $TOKEN" \\
          -H "Content-Type: application/json" \\
          -d '{"ticker":"SNTS","action":"buy","quantity":100,
               "unit_price":14250,"reason":"brief","brief_id":42}'
    """
    executed_at = payload.executed_at or datetime.now(UTC)

    with get_session() as s:
        # Validation croisée optionnelle des FK (évite les refs mortes)
        if payload.brief_id is not None and s.get(Brief, payload.brief_id) is None:
            raise HTTPException(
                status_code=422, detail=f"brief_id={payload.brief_id} introuvable"
            )
        if payload.signal_id is not None:
            sig = s.get(Signal, payload.signal_id)
            if sig is None:
                raise HTTPException(
                    status_code=422, detail=f"signal_id={payload.signal_id} introuvable"
                )
            # Cohérence : si on fournit les deux, signal doit appartenir au brief
            if payload.brief_id is not None and sig.brief_id != payload.brief_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"signal_id={payload.signal_id} n'appartient pas au brief_id={payload.brief_id}",
                )

        trade = Trade(
            ticker=payload.ticker,
            action=payload.action,
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            executed_at=executed_at,
            reason=payload.reason,
            brief_id=payload.brief_id,
            signal_id=payload.signal_id,
            notes=payload.notes,
        )
        s.add(trade)
        s.flush()
        s.refresh(trade)
        return TradeOut.model_validate(trade)


@router.get("", response_model=PaginatedResponse[TradeOut])
def list_trades(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    ticker: str | None = None,
    reason: Reason | None = None,
):
    """Liste les trades avec filtrage ticker/reason + pagination."""
    with get_session() as s:
        stmt = select(Trade).order_by(Trade.executed_at.desc())
        if ticker:
            stmt = stmt.where(Trade.ticker == ticker.strip().upper())
        if reason:
            stmt = stmt.where(Trade.reason == reason)
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[TradeOut](
            items=[TradeOut.model_validate(t) for t in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/{trade_id}", response_model=TradeOut)
def get_trade(trade_id: int):
    with get_session() as s:
        trade = s.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="Trade introuvable")
        return TradeOut.model_validate(trade)


@router.patch("/{trade_id}", response_model=TradeOut)
def patch_trade(trade_id: int, payload: TradePatch):
    """Enrichir un trade a posteriori (attribution à un brief/signal, notes)."""
    with get_session() as s:
        trade = s.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="Trade introuvable")
        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(trade, field, value)
        s.flush()
        s.refresh(trade)
        return TradeOut.model_validate(trade)


@router.delete("/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trade(trade_id: int):
    """Supprime un trade saisi par erreur. Pas de soft-delete : le registre
    reflète la réalité, une saisie erronée est à effacer pour ne pas polluer
    le backtest."""
    with get_session() as s:
        trade = s.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="Trade introuvable")
        s.delete(trade)
