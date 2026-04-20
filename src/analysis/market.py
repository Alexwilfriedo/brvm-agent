"""Analyse marché BRVM — snapshot agrégé + narrative Sonnet.

Deux fonctions clés :
  - `build_snapshot(session, trading_date)` : agrège les `Quote` du jour en un
    dict prêt pour le front (top movers, secteurs, heatmap).
  - `generate_analysis(session, trading_date)` : cache-first, sinon appelle
    Sonnet avec le snapshot + contexte pour produire une narrative FR.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from anthropic import Anthropic, APIError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..collectors.sika_quotes import BRVM_TICKERS, Listed
from ..config import get_settings
from ..models import MarketAnalysis, NewsArticle, Quote
from ._retry import anthropic_retry
from .enrichment import _strip_fence

logger = logging.getLogger(__name__)


# --- Snapshot ---------------------------------------------------------------

def _latest_trading_day(session: Session) -> datetime | None:
    """Date de la dernière séance cotée (la plus récente `quote_date` en DB)."""
    return session.execute(
        select(Quote.quote_date).order_by(Quote.quote_date.desc()).limit(1)
    ).scalar_one_or_none()


def _quote_to_row(q: Quote) -> dict:
    return {
        "ticker": q.ticker,
        "name": q.name,
        "sector": q.sector or "",
        "country": q.country or "",
        "close_price": q.close_price,
        "variation_pct": q.variation_pct,
        "volume": q.volume,
        "value_traded": q.value_traded,
        "extras": q.extras or {},
    }


def build_snapshot(session: Session, trading_date: datetime | None = None) -> dict:
    """Construit le snapshot marché du jour (ou de la date fournie).

    Retour :
      {
        "trading_date": ISO,
        "quotes_count": int,
        "movers_up": [top 5],
        "movers_down": [top 5],
        "top_volumes": [top 5],
        "top_values": [top 5],
        "by_sector": [{sector, count, avg_var, total_value}],
        "all_quotes": [...]  # pour la heatmap
      }
    """
    if trading_date is None:
        trading_date = _latest_trading_day(session)
    if trading_date is None:
        return {"trading_date": None, "quotes_count": 0}

    # Normalise au jour
    day = trading_date.replace(hour=0, minute=0, second=0, microsecond=0)

    quotes: list[Quote] = list(
        session.execute(
            select(Quote).where(Quote.quote_date == day).order_by(Quote.ticker)
        ).scalars().all()
    )
    rows = [_quote_to_row(q) for q in quotes]
    total_value = sum(r["value_traded"] for r in rows)

    # Tri : on exclut les titres sans cotation (close=0 ET volume=0)
    traded = [r for r in rows if r["close_price"] > 0 and r["volume"] > 0]
    by_var_desc = sorted(traded, key=lambda r: r["variation_pct"], reverse=True)
    by_var_asc = sorted(traded, key=lambda r: r["variation_pct"])
    by_volume = sorted(rows, key=lambda r: r["volume"], reverse=True)
    by_value = sorted(rows, key=lambda r: r["value_traded"], reverse=True)

    # Agrégats par secteur
    sectors: dict[str, dict] = {}
    for r in rows:
        sec = r["sector"] or "Autres"
        s = sectors.setdefault(sec, {"sector": sec, "count": 0, "sum_var": 0.0, "total_value": 0.0, "traded_count": 0})
        s["count"] += 1
        s["total_value"] += r["value_traded"]
        if r["close_price"] > 0 and r["volume"] > 0:
            s["sum_var"] += r["variation_pct"]
            s["traded_count"] += 1
    by_sector = []
    for s in sectors.values():
        avg_var = round(s["sum_var"] / s["traded_count"], 3) if s["traded_count"] else 0.0
        by_sector.append({
            "sector": s["sector"],
            "count": s["count"],
            "traded_count": s["traded_count"],
            "avg_var_pct": avg_var,
            "total_value": s["total_value"],
        })
    by_sector.sort(key=lambda x: -x["total_value"])

    return {
        "trading_date": day.isoformat(),
        "quotes_count": len(rows),
        "traded_count": len(traded),
        "total_value": total_value,
        "movers_up": by_var_desc[:5],
        "movers_down": by_var_asc[:5],
        "top_volumes": by_volume[:5],
        "top_values": by_value[:5],
        "by_sector": by_sector,
        "all_quotes": rows,
    }


# --- Analyse Sonnet ---------------------------------------------------------

_ANALYSIS_SYSTEM = """Tu es un analyste sell-side senior BRVM/UEMOA (15+ ans).
Tu rédiges une interprétation de la séance à partir de données cotées + \
un contexte minimal.

Sortie attendue : **JSON strict** (pas de markdown autour) :

{
  "headline": "1 phrase punch — ce qui caractérise la séance",
  "market_summary": "3-5 phrases denses, factuelles, avec chiffres. Mentionne le régime \
(haussier/baissier/range), les secteurs moteurs, et 1-2 valeurs qui se distinguent.",
  "sector_highlights": [
    {"sector": "Banque", "takeaway": "1 phrase — ce qui ressort sur ce secteur"}
  ],
  "signals": [
    "Signal technique ou opérationnel observable sur un ou plusieurs tickers"
  ],
  "watchlist": [
    {"ticker": "XXX", "reason": "1 phrase — pourquoi surveiller ce ticker"}
  ]
}

Règles :
- Zéro certitude factice. Langage probabiliste.
- Pas de recommandation d'achat/vente — c'est de l'analyse descriptive.
- Français. JSON pur. 5 signals max, 5 watchlist max, 4 sector_highlights max.
- Se limite aux titres vraiment liquides (volume > 500, écarte les autres).
"""


def _get_previous_analyses(session: Session, limit: int = 3) -> list[dict]:
    """Récupère les N dernières analyses pour donner du contexte à Sonnet."""
    rows = session.execute(
        select(MarketAnalysis).order_by(MarketAnalysis.trading_date.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "date": r.trading_date.date().isoformat(),
            "headline": (r.key_stats or {}).get("headline", ""),
            "summary": r.narrative_fr[:300],
        }
        for r in rows
    ]


@anthropic_retry
def _call_sonnet(payload: dict) -> tuple[str, int, int]:
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.model_enrichment,  # Sonnet — moins cher que Opus pour cette tâche
        max_tokens=1500,
        system=_ANALYSIS_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                "Analyse la séance BRVM ci-dessous. Retourne le JSON demandé.\n\n"
                f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}"
            ),
        }],
    )
    return (
        resp.content[0].text,
        int(resp.usage.input_tokens) if resp.usage else 0,
        int(resp.usage.output_tokens) if resp.usage else 0,
    )


def generate_analysis(
    session: Session,
    trading_date: datetime | None = None,
    *,
    force: bool = False,
) -> MarketAnalysis | None:
    """Retourne l'analyse pour `trading_date` (cache-first).

    Si `force=True` ou si pas encore d'analyse en DB, on appelle Sonnet.
    """
    settings = get_settings()
    if trading_date is None:
        trading_date = _latest_trading_day(session)
    if trading_date is None:
        return None

    day = trading_date.replace(hour=0, minute=0, second=0, microsecond=0)

    existing = session.execute(
        select(MarketAnalysis).where(MarketAnalysis.trading_date == day)
    ).scalar_one_or_none()
    if existing and not force:
        return existing

    snapshot = build_snapshot(session, day)
    if snapshot.get("quotes_count", 0) == 0:
        return None

    # Historique compact des analyses précédentes (évite Sonnet de se répéter)
    history = _get_previous_analyses(session, limit=3)

    payload = {
        "trading_date": snapshot["trading_date"],
        "total_value_fcfa": snapshot["total_value"],
        "traded_count": snapshot["traded_count"],
        "movers_up": snapshot["movers_up"],
        "movers_down": snapshot["movers_down"],
        "top_values": snapshot["top_values"],
        "by_sector": snapshot["by_sector"],
        "previous_sessions": history,
    }

    try:
        raw, in_tok, out_tok = _call_sonnet(payload)
        data = json.loads(_strip_fence(raw))
    except (APIError, json.JSONDecodeError) as e:
        logger.exception(f"Analyse marché Sonnet échouée : {e}")
        return None

    narrative = data.get("market_summary", "") or ""
    key_stats = {
        **data,
        # On embarque aussi les snapshots pour que l'UI puisse lire l'analyse hors
        # de /snapshot si elle veut (snapshot = source of truth, key_stats = dérivé).
        "snapshot_ref": {
            "total_value": snapshot["total_value"],
            "traded_count": snapshot["traded_count"],
        },
    }

    if existing:
        existing.narrative_fr = narrative
        existing.key_stats = key_stats
        existing.model_used = settings.model_enrichment
        existing.input_tokens = in_tok
        existing.output_tokens = out_tok
        existing.generated_at = datetime.now(UTC)
        session.flush()
        return existing

    analysis = MarketAnalysis(
        trading_date=day,
        narrative_fr=narrative,
        key_stats=key_stats,
        model_used=settings.model_enrichment,
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
    session.add(analysis)
    session.flush()
    return analysis


# --- Ticker detail ----------------------------------------------------------

# Référentiel BRVM accessible par ticker — utilisé pour retrouver name/sector/country
# même si le ticker n'a aucun Quote en DB (première session).
_TICKERS_BY_CODE: dict[str, Listed] = {t.ticker: t for t in BRVM_TICKERS}


def _perf_from_series(closes: list[float], days_ago: int) -> float | None:
    """Perf % entre la dernière valeur et celle d'il y a `days_ago` points.

    On s'appuie sur l'index (une cotation par jour de bourse) plutôt que sur
    la date calendaire — suffisamment précis pour afficher une perf.
    """
    if len(closes) <= days_ago:
        return None
    current = closes[-1]
    past = closes[-1 - days_ago]
    if not past:
        return None
    return round((current - past) / past * 100, 3)


def build_ticker_detail(
    session: Session,
    ticker: str,
    *,
    days: int = 90,
    news_limit: int = 10,
) -> dict | None:
    """Construit la fiche détail complète d'un ticker.

    Retour :
      {
        ticker, name, sector, country,
        latest: { quote_date, close_price, variation_pct, volume, value_traded, extras },
        series: [{date, close, volume, variation, open, high, low}] asc,
        stats: { high_52w, low_52w, avg_volume_30d, perf_1d/7d/30d/90d, series_days },
        news:  [{id, title, url, published_at, source_key, summary,
                 sentiment, materiality, themes}]
      }
    `None` si le ticker n'est ni dans le référentiel ni en DB.
    """
    ticker_u = ticker.upper()
    meta = _TICKERS_BY_CODE.get(ticker_u)

    # Historique (borné) — ordonné ascendant pour construire la série
    since = datetime.now(UTC) - timedelta(days=max(1, days))
    rows: list[Quote] = list(
        session.execute(
            select(Quote)
            .where(Quote.ticker == ticker_u)
            .where(Quote.quote_date >= since)
            .order_by(Quote.quote_date.asc())
        ).scalars().all()
    )

    if not rows and not meta:
        # Ticker inconnu et sans historique → 404 côté API
        return None

    # Si on n'a pas de méta référentiel, on reconstruit depuis la 1re ligne DB
    if not meta and rows:
        first = rows[0]
        name = first.name
        sector = first.sector or ""
        country = first.country or ""
    else:
        assert meta is not None
        name = meta.name
        sector = meta.sector
        country = meta.country

    # Série + stats
    series: list[dict] = []
    closes: list[float] = []
    volumes: list[int] = []
    for q in rows:
        extras = q.extras or {}
        series.append({
            "date": q.quote_date.isoformat(),
            "close": q.close_price,
            "volume": q.volume,
            "variation_pct": q.variation_pct,
            "open": extras.get("open_price"),
            "high": extras.get("high_price"),
            "low": extras.get("low_price"),
            "previous_close": extras.get("previous_close"),
        })
        if q.close_price and q.close_price > 0:
            closes.append(q.close_price)
        volumes.append(q.volume)

    stats: dict = {
        "series_days": len(series),
        "high_52w": max(closes) if closes else None,
        "low_52w": min(closes) if closes else None,
        "avg_volume_30d": round(sum(volumes[-30:]) / max(1, len(volumes[-30:])))
        if volumes else None,
        # Perfs basées sur l'index (1 point par séance). Demande des données…
        "perf_1d": _perf_from_series(closes, 1),
        "perf_7d": _perf_from_series(closes, 5),   # ~5 séances de bourse/semaine
        "perf_30d": _perf_from_series(closes, 22), # ~22 séances par mois
        "perf_90d": _perf_from_series(closes, 66),
    }

    # Dernière cotation — None si aucune ligne (ticker référencé mais pas encore scrapé)
    if rows:
        last = rows[-1]
        latest = {
            "quote_date": last.quote_date.isoformat(),
            "close_price": last.close_price,
            "variation_pct": last.variation_pct,
            "volume": last.volume,
            "value_traded": last.value_traded,
            "extras": last.extras or {},
        }
    else:
        latest = None

    # News mentionnant ce ticker. La colonne est `JSON` (pas `JSONB`), et le
    # dialecte Postgres refuse `LIKE` / `.contains([...])` directement sur un
    # JSON. On fait simple et portable : fetch les dernières news enrichies
    # dans une fenêtre raisonnable, puis filtre en Python. Volume attendu : au
    # plus quelques centaines d'articles enrichis sur 30j.
    news: list[dict] = []
    news_since = datetime.now(UTC) - timedelta(days=60)
    candidates = session.execute(
        select(NewsArticle)
        .where(NewsArticle.enriched_at.isnot(None))
        .where(NewsArticle.published_at >= news_since)
        .order_by(NewsArticle.published_at.desc().nullslast())
        .limit(500)
    ).scalars().all()

    news_rows = [
        n for n in candidates
        if ticker_u in (n.tickers_mentioned or [])
    ][:news_limit]

    for n in news_rows:
        enr = n.enrichment or {}
        news.append({
            "id": n.id,
            "title": n.title,
            "url": n.url,
            "source_key": n.source_key,
            "published_at": n.published_at.isoformat() if n.published_at else None,
            "summary": n.summary,
            "sentiment": enr.get("sentiment"),
            "materiality": enr.get("materiality"),
            "themes": enr.get("themes", []),
        })

    return {
        "ticker": ticker_u,
        "name": name,
        "sector": sector,
        "country": country,
        "latest": latest,
        "series": series,
        "stats": stats,
        "news": news,
    }


__all__ = ["build_snapshot", "build_ticker_detail", "generate_analysis"]


# Suppress unused import
_ = Any
_ = func
