"""Indicateurs techniques simples pour enrichir le contexte envoyé à Opus.

Implémenté en stdlib Python (pas de pandas/numpy) — BRVM est small-data
(~60 points par ticker), la complexité pandas n'apporte rien et ajouterait
60 MB sur l'image Railway. Toutes les fonctions tolèrent les séries courtes
en retournant `None` plutôt que de lever, pour que Opus reçoive juste
l'information disponible.

Features calculées :
  - `ma20`, `ma50` : moyennes mobiles simples
  - `ma_trend` : 'haussier' si MA20 > MA50, 'baissier' si <, None sinon
  - `bollinger_position` : z-score du dernier close vs MA20 (nb d'écarts-types)
  - `atr_pct` : ATR(14) normalisé en % du close (proxy volatilité journalière)
  - `volume_ratio_20` : volume du dernier jour / moyenne volume 20j
  - `pct_from_52w_high`, `pct_from_52w_low` : distance en % des extrêmes annuels
  - `momentum_1w_pct`, `momentum_1m_pct` : returns 5j, 20j
"""
from __future__ import annotations

import statistics
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Quote


# Minimum d'observations pour calculer un indicateur. En dessous on renvoie None.
_MIN_FOR_MA20 = 20
_MIN_FOR_MA50 = 50
_MIN_FOR_BOLLINGER = 20  # même fenêtre que MA20 par convention
_MIN_FOR_ATR = 14
_MIN_FOR_52W = 30  # on accepte une estimation dès 30 jours


def _safe_stdev(values: list[float]) -> float | None:
    """Écart-type bootstrapé — stdev() lève sur < 2 points, on veut juste None."""
    if len(values) < 2:
        return None
    try:
        return statistics.stdev(values)
    except statistics.StatisticsError:
        return None


def _compute_atr_pct(quotes: list[Quote]) -> float | None:
    """ATR(14) en % du close, basé sur high/low/close stockés dans extras.

    True Range = max(high - low, |high - prev_close|, |low - prev_close|).
    ATR = moyenne simple des 14 derniers TR (pas de EMA — simplicité).
    Retourne le ratio ATR/close × 100 pour avoir une volatilité comparable
    entre tickers de prix très différents (BOAC ~7k vs NTLC ~200k).
    """
    if len(quotes) < _MIN_FOR_ATR + 1:
        return None

    trs: list[float] = []
    for i in range(1, len(quotes)):
        cur = quotes[i]
        prev = quotes[i - 1]
        extras_cur = cur.extras or {}
        high = extras_cur.get("high")
        low = extras_cur.get("low")
        prev_close = prev.close_price
        if not (isinstance(high, (int, float)) and isinstance(low, (int, float))
                and isinstance(prev_close, (int, float))):
            continue
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(float(tr))

    if len(trs) < _MIN_FOR_ATR:
        return None

    atr = sum(trs[-_MIN_FOR_ATR:]) / _MIN_FOR_ATR
    last_close = quotes[-1].close_price
    if not last_close or last_close <= 0:
        return None
    return round(atr / last_close * 100, 2)


def compute_technical_features(ticker: str, session: Session) -> dict:
    """Charge ~60 dernières quotes et retourne un dict d'indicateurs techniques.

    Toutes les valeurs sont optionnelles — Opus voit ce qu'on a pu calculer.
    Pas d'exception levée, jamais : en pipeline prod on préfère un dict vide
    qu'un crash qui planterait le brief entier.
    """
    quotes = list(session.execute(
        select(Quote)
        .where(Quote.ticker == ticker)
        .order_by(Quote.quote_date.desc())
        .limit(60)
    ).scalars().all())
    # Order chronologiquement pour itérer naturellement (ancien → récent)
    quotes = list(reversed(quotes))
    closes = [q.close_price for q in quotes if q.close_price and q.close_price > 0]

    if len(closes) < 5:
        # Série trop courte — on ne peut rien calculer de stable
        return {}

    last_close = closes[-1]
    feat: dict = {}

    # --- Moving averages + trend --------------------------------------------
    if len(closes) >= _MIN_FOR_MA20:
        ma20 = sum(closes[-20:]) / 20
        feat["ma20"] = round(ma20, 2)
        feat["pct_vs_ma20"] = round((last_close - ma20) / ma20 * 100, 2)
    if len(closes) >= _MIN_FOR_MA50:
        ma50 = sum(closes[-50:]) / 50
        feat["ma50"] = round(ma50, 2)
        feat["pct_vs_ma50"] = round((last_close - ma50) / ma50 * 100, 2)

    if "ma20" in feat and "ma50" in feat:
        if feat["ma20"] > feat["ma50"] * 1.005:   # +0,5% de marge pour éviter le bruit
            feat["ma_trend"] = "haussier"
        elif feat["ma20"] < feat["ma50"] * 0.995:
            feat["ma_trend"] = "baissier"
        else:
            feat["ma_trend"] = "neutre"

    # --- Bollinger (position normalisée) ------------------------------------
    if len(closes) >= _MIN_FOR_BOLLINGER:
        window = closes[-20:]
        mean = sum(window) / 20
        std = _safe_stdev(window)
        if std and std > 0:
            feat["bollinger_position"] = round((last_close - mean) / std, 2)

    # --- ATR en % du close --------------------------------------------------
    atr_pct = _compute_atr_pct(quotes)
    if atr_pct is not None:
        feat["atr_pct"] = atr_pct

    # --- Volume ratio -------------------------------------------------------
    volumes = [q.volume for q in quotes if q.volume and q.volume > 0]
    if len(volumes) >= _MIN_FOR_MA20:
        avg_vol_20 = sum(volumes[-20:]) / 20
        last_vol = volumes[-1]
        if avg_vol_20 > 0:
            feat["volume_ratio_20"] = round(last_vol / avg_vol_20, 2)

    # --- 52w high/low -------------------------------------------------------
    if len(closes) >= _MIN_FOR_52W:
        # On borne à 252 séances (~1 an de trading)
        window_52w = closes[-252:] if len(closes) > 252 else closes
        high_52w = max(window_52w)
        low_52w = min(window_52w)
        if high_52w > 0:
            feat["pct_from_52w_high"] = round((last_close - high_52w) / high_52w * 100, 2)
        if low_52w > 0:
            feat["pct_from_52w_low"] = round((last_close - low_52w) / low_52w * 100, 2)

    # --- Momentum -----------------------------------------------------------
    if len(closes) >= 6:
        ref = closes[-6]
        if ref > 0:
            feat["momentum_1w_pct"] = round((last_close - ref) / ref * 100, 2)
    if len(closes) >= 21:
        ref = closes[-21]
        if ref > 0:
            feat["momentum_1m_pct"] = round((last_close - ref) / ref * 100, 2)

    # Nombre d'observations utilisées (utile à Opus pour pondérer sa confiance)
    feat["history_days"] = len(closes)
    return feat


# --- Sector rotation helper -------------------------------------------------

def compute_sector_rotation(session: Session, lookback_days: int = 5) -> dict[str, float]:
    """Return moyen pondéré par secteur sur `lookback_days` dernières séances.

    Permet à Opus de voir que les banques ont fait +2,3% alors que les télécoms
    ont fait -1,1% la semaine passée → argument sectoriel direct.

    Retourne un dict `{sector: avg_return_pct}` — secteurs sans quotes skippés.
    """
    # On prend les quotes des 2× lookback derniers jours pour avoir une marge
    # (jours non-ouvrés, gaps…).
    from datetime import UTC, datetime
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days * 2)

    rows = session.execute(
        select(Quote.ticker, Quote.sector, Quote.close_price, Quote.quote_date)
        .where(Quote.quote_date >= cutoff)
        .where(Quote.close_price.is_not(None))
        .order_by(Quote.ticker, Quote.quote_date)
    ).all()

    # Groupe par ticker, garde première/dernière close
    by_ticker: dict[str, list] = {}
    for ticker, sector, close, qd in rows:
        by_ticker.setdefault(ticker, []).append({
            "sector": sector, "close": float(close), "date": qd,
        })

    sector_returns: dict[str, list[float]] = {}
    for ticker, obs in by_ticker.items():
        if len(obs) < 2:
            continue
        first, last = obs[0], obs[-1]
        if first["close"] <= 0:
            continue
        ret = (last["close"] - first["close"]) / first["close"] * 100
        sector = first["sector"] or "Autre"
        sector_returns.setdefault(sector, []).append(ret)

    out: dict[str, float] = {}
    for sector, returns in sector_returns.items():
        if not returns:
            continue
        out[sector] = round(sum(returns) / len(returns), 2)
    return out
