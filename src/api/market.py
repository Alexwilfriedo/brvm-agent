"""Endpoints marché : snapshot agrégé + analyse Sonnet cachée."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..analysis.market import (
    build_pulse,
    build_pulse_history,
    build_snapshot,
    build_ticker_detail,
    generate_analysis,
)
from ..collectors.historical_import import (
    ImportCsvError,
    parse_historical_csv,
)
from ..collectors.sika_quotes import BRVM_TICKERS
from ..database import get_session
from ..models import Quote
from .deps import require_admin

logger = logging.getLogger(__name__)

# Set des tickers BRVM connus — utilisé pour valider /tickers/{t}/import avant
# de toucher la DB. Construit une fois au chargement (BRVM_TICKERS est stable).
_KNOWN_TICKERS: frozenset[str] = frozenset(t.ticker.upper() for t in BRVM_TICKERS)

router = APIRouter(prefix="/api/market", tags=["market"], dependencies=[Depends(require_admin)])


@router.get("/pulse")
def get_pulse():
    """Pulse synthétique du marché (hero dashboard)."""
    with get_session() as s:
        return build_pulse(s)


@router.get("/pulse/history")
def get_pulse_history(
    days: int = Query(7, ge=1, le=90, description="Nombre de séances dans la sparkline"),
):
    """Série journalière `{date, variation_pct_weighted, total_value}` pour sparkline."""
    with get_session() as s:
        return build_pulse_history(s, days=days)


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


# --- Import historique CSV --------------------------------------------------


# Cap anti-abus : un fichier > 5 MB n'a pas de raison d'exister pour un historique
# BRVM (quelques kB par ticker pour 5-10 ans). Évite DoS via upload massif.
_MAX_CSV_BYTES = 5 * 1024 * 1024


class ImportQuotesOut(BaseModel):
    """Résultat de `POST /api/market/tickers/{ticker}/import`."""
    ticker: str
    inserted: int
    updated: int
    skipped: int
    total_rows_in_file: int
    earliest_date: datetime | None
    latest_date: datetime | None
    detected_delimiter: str
    detected_columns: dict[str, str]
    errors: list[str]


@router.post("/tickers/{ticker}/import", response_model=ImportQuotesOut)
async def import_ticker_history(
    ticker: str,
    file: UploadFile = File(..., description="Fichier CSV d'historique"),
):
    """Backfill l'historique d'un ticker depuis un CSV uploadé.

    Colonnes minimales : `date`, `close`. Colonnes optionnelles reconnues :
    `open`, `high`, `low`, `volume`, `value_traded`, `variation_pct`.
    L'en-tête est case-insensitive et accepte les alias FR/EN courants
    (clôture, séance, titres, etc.). Séparateurs `,` `;` `\\t` et décimales
    `.` ou `,` sont auto-détectés.

    Upsert sur `(ticker, quote_date)` : ré-importer le même CSV met à jour
    les lignes existantes, ne crée pas de doublons. `name`/`sector`/`country`
    sont préservés s'ils existent déjà en base (on les remplit seulement si
    le ticker est nouveau).
    """
    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 16:
        raise HTTPException(status_code=400, detail=f"Ticker invalide : {ticker!r}")
    # Valide contre le référentiel BRVM avant tout travail — évite de créer
    # des lignes `quotes` orphelines avec name/sector vides pour un ticker
    # qui n'existe pas réellement.
    if ticker not in _KNOWN_TICKERS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Ticker {ticker!r} inconnu du référentiel BRVM. "
                "Vérifie l'orthographe ou ajoute-le dans BRVM_TICKERS."
            ),
        )

    # Lecture streaming bornée — évite de charger un fichier géant en RAM
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Fichier vide.")
    if len(content) > _MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Fichier trop gros (> {_MAX_CSV_BYTES // (1024 * 1024)} MB).",
        )

    try:
        result = parse_historical_csv(content)
    except ImportCsvError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not result.rows:
        # Parser a collecté que des erreurs → rien à insérer, on renvoie 400
        # avec le détail pour que l'utilisateur corrige son CSV.
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Aucune ligne exploitable dans le fichier.",
                "errors": result.errors[:20],
            },
        )

    # Persistance bulk upsert --------------------------------------------------
    inserted = 0
    updated = 0

    with get_session() as s:
        # Récup meta ticker existant pour préserver name/sector/country.
        existing = s.execute(
            select(Quote).where(Quote.ticker == ticker).order_by(Quote.quote_date.desc()).limit(1)
        ).scalar_one_or_none()
        default_name = existing.name if existing else ""
        default_sector = existing.sector if existing else None
        default_country = existing.country if existing else None

        # Set des dates déjà en base pour ce ticker (pour compter inserted vs updated).
        existing_dates = {
            d for (d,) in s.execute(
                select(Quote.quote_date).where(Quote.ticker == ticker)
            ).all()
        }

        rows_sql = []
        for r in result.rows:
            extras: dict = {}
            if r.open_price is not None:
                extras["open_price"] = r.open_price
            if r.high_price is not None:
                extras["high_price"] = r.high_price
            if r.low_price is not None:
                extras["low_price"] = r.low_price

            rows_sql.append({
                "ticker": ticker,
                "name": default_name,
                "sector": default_sector,
                "country": default_country,
                "quote_date": r.quote_date,
                "close_price": r.close_price,
                "variation_pct": r.variation_pct if r.variation_pct is not None else 0.0,
                "volume": r.volume,
                "value_traded": r.value_traded,
                "extras": extras,
            })

            if r.quote_date in existing_dates:
                updated += 1
            else:
                inserted += 1

        if rows_sql:
            # Postgres ON CONFLICT DO UPDATE sur l'index unique (ticker, quote_date).
            stmt = pg_insert(Quote).values(rows_sql)
            excluded = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=["ticker", "quote_date"],
                set_={
                    "close_price": excluded.close_price,
                    "variation_pct": excluded.variation_pct,
                    "volume": excluded.volume,
                    "value_traded": excluded.value_traded,
                    # Merge extras : on préserve les clés existantes (ex: PER, RSI
                    # scrapés par le cron) et on ajoute open/high/low du CSV.
                    "extras": Quote.__table__.c.extras.op("||")(excluded.extras),
                },
            )
            s.execute(stmt)

    dates = [r.quote_date for r in result.rows]
    logger.info(
        f"[import] {ticker}: inserted={inserted} updated={updated} "
        f"skipped={result.skipped} errors={len(result.errors)}",
    )
    return ImportQuotesOut(
        ticker=ticker,
        inserted=inserted,
        updated=updated,
        skipped=result.skipped,
        total_rows_in_file=len(result.rows) + result.skipped,
        earliest_date=min(dates) if dates else None,
        latest_date=max(dates) if dates else None,
        detected_delimiter=result.detected_delimiter,
        detected_columns=result.detected_columns,
        errors=result.errors[:20],  # cap pour éviter de gonfler la réponse
    )
