"""Analyse d'investissement on-demand par Opus pour un ticker donné.

Répond à une seule question : **faut-il investir sur ce ticker, oui ou non,
et pourquoi ?** Agrège les données déjà présentes en DB (dernière quote +
features techniques + news enrichies mentionnant le ticker + signaux passés
+ rotation sectorielle) et les envoie à Opus avec un prompt dédié. Réponse
JSON strict, stockée dans `investment_analyses` pour évaluation a posteriori.

Design notes :
- **Pas de pipeline_run** : l'audit-trail est porté par la table dédiée.
- **Pas de scheduler** : endpoint à la demande, pas de cron.
- **Dédup 15min** : appelé via le router (`_get_cached_recent`) pour protéger
  le budget Opus contre les double-clicks / boucles UI.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from anthropic import Anthropic, APIError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Brief, NewsArticle, Quote, Signal
from ._retry import anthropic_retry
from .enrichment import _capture_llm_error, _strip_fence
from .features import compute_sector_rotation, compute_technical_features

logger = logging.getLogger(__name__)

Horizon = Literal["short", "medium", "long"]

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "invest_decision.md"

# Fenêtres d'historique selon l'horizon demandé (news lookback en jours).
_HORIZON_CONFIG: dict[str, dict] = {
    "short": {
        "description": "court terme (3 à 15 jours)",
        "news_lookback_days": 14,
        "min_days": 3,
        "max_days": 15,
    },
    "medium": {
        "description": "moyen terme (16 à 90 jours)",
        "news_lookback_days": 45,
        "min_days": 16,
        "max_days": 90,
    },
    "long": {
        "description": "long terme (91 à 365 jours)",
        "news_lookback_days": 120,
        "min_days": 91,
        "max_days": 365,
    },
}

# Garde-fous contre les hallucinations de price_target.
_PRICE_TARGET_MIN_RATIO = 0.5
_PRICE_TARGET_MAX_RATIO = 2.0

# Tolérance sur price_at_analysis renvoyé par Opus (doit ≈ dernier close DB).
_PRICE_SNAPSHOT_TOLERANCE_PCT = 0.5


@dataclass
class InvestmentAnalysisResult:
    """Sortie de `InvestmentAdvisor.analyze`. Toujours valide — en cas d'erreur
    Opus, on retourne un payload `hold` dégradé plutôt que de lever."""

    recommendation: str  # "buy" | "hold" | "avoid"
    confidence: float
    price_at_analysis: float
    price_target: float | None
    stop_loss: float | None
    time_horizon_days: int | None
    payload: dict
    input_tokens: int
    output_tokens: int
    model_used: str


class InvestmentAdvisor:
    """Service stateless (sauf DB) qui produit une analyse d'investissement."""

    def __init__(self, model: str | None = None):
        self.settings = get_settings()
        self.client = Anthropic(api_key=self.settings.anthropic_api_key)
        # Réutilise model_synthesis (Opus) par défaut — cohérent avec la synthèse
        # quotidienne. Override possible pour A/B test.
        self.model = model or self.settings.model_synthesis
        template = PROMPT_PATH.read_text(encoding="utf-8")
        self._prompt_template = template.replace(
            "{{investor_profile}}", self.settings.investor_profile,
        )

    # --- Public API ---------------------------------------------------------

    def analyze(
        self,
        ticker: str,
        horizon: Horizon,
        session: Session,
    ) -> InvestmentAnalysisResult:
        """Produit une recommandation pour `ticker` sur `horizon`.

        Précondition : le ticker DOIT exister dans `quotes` (validé côté router
        avant appel — évite de cramer des tokens pour rien).
        """
        if horizon not in _HORIZON_CONFIG:
            raise ValueError(f"Horizon invalide : {horizon!r}")

        ticker = ticker.strip().upper()
        config = _HORIZON_CONFIG[horizon]

        # 1. Charger le contexte (quotes, features, news, signals, rotation)
        context = self._build_context(
            ticker=ticker,
            news_lookback_days=config["news_lookback_days"],
            session=session,
        )
        if context["ticker_info"] is None:
            # Défensif : le router valide déjà, mais on ne crash pas si un race
            # a supprimé le ticker entre-temps.
            return _degraded_result(
                reason="ticker_absent_from_quotes",
                model=self.model,
                price_at_analysis=0.0,
            )

        current_price = float(context["ticker_info"]["close_price"])

        # 2. Construire le system prompt avec l'horizon substitué
        system_prompt = self._prompt_template.replace(
            "{{horizon}}", horizon,
        ).replace(
            "{{horizon_description}}", config["description"],
        )

        user_content = (
            "Voici les données d'entrée pour l'analyse. Réponds avec le JSON "
            "strict demandé.\n\n"
            f"{json.dumps(context, ensure_ascii=False, default=str, indent=2)}"
        )

        # 3. Appel LLM (retry sur transient errors)
        raw = ""
        input_tokens = 0
        output_tokens = 0
        try:
            raw, input_tokens, output_tokens = self._call_llm(system_prompt, user_content)
        except APIError as e:
            logger.exception("InvestmentAdvisor: appel Anthropic échoué")
            _capture_llm_error(
                e, step="invest_decision", model=self.model,
                error_kind="anthropic_api", ticker=ticker, horizon=horizon,
            )
            return _degraded_result(
                reason=f"anthropic_api: {e}",
                model=self.model,
                price_at_analysis=current_price,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("InvestmentAdvisor: erreur inattendue")
            _capture_llm_error(
                e, step="invest_decision", model=self.model,
                error_kind="unknown", ticker=ticker, horizon=horizon,
            )
            return _degraded_result(
                reason=f"unknown: {e}",
                model=self.model,
                price_at_analysis=current_price,
            )

        # 4. Parse JSON strict
        try:
            data = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            logger.error(
                f"InvestmentAdvisor JSON invalide pour {ticker}/{horizon}: "
                f"{e} — raw[:300]={raw[:300]!r}",
            )
            _capture_llm_error(
                e, step="invest_decision", model=self.model,
                error_kind="invalid_json", ticker=ticker, horizon=horizon,
            )
            return _degraded_result(
                reason=f"invalid_json: {e}",
                model=self.model,
                price_at_analysis=current_price,
                raw_preview=raw[:500],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        # 5. Anti-hallucination : snap le prix et borne le target
        data = _sanitize_prices(
            data,
            current_price=current_price,
            horizon=horizon,
            model=self.model,
            ticker=ticker,
        )

        # 6. Construction du résultat
        recommendation = _coerce_recommendation(data.get("recommendation"))
        confidence = _clamp_confidence(data.get("confidence"))
        price_target = _safe_float(data.get("price_target"))
        stop_loss = _safe_float(data.get("stop_loss"))
        time_horizon_days = _clamp_horizon_days(
            data.get("time_horizon_days"),
            horizon=horizon,
        )

        return InvestmentAnalysisResult(
            recommendation=recommendation,
            confidence=confidence,
            price_at_analysis=current_price,
            price_target=price_target,
            stop_loss=stop_loss,
            time_horizon_days=time_horizon_days,
            payload=data,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_used=self.model,
        )

    # --- LLM call -----------------------------------------------------------

    @anthropic_retry
    def _call_llm(self, system_prompt: str, user_content: str) -> tuple[str, int, int]:
        """Retourne (texte brut, input_tokens, output_tokens)."""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=3000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = resp.content[0].text
        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        return text, input_tokens, output_tokens

    # --- Context building ---------------------------------------------------

    def _build_context(
        self,
        *,
        ticker: str,
        news_lookback_days: int,
        session: Session,
    ) -> dict:
        """Compose le payload d'entrée pour Opus."""
        ticker_info = _load_ticker_info(ticker, session)
        technical = compute_technical_features(ticker, session) if ticker_info else {}
        news_items = _load_recent_news_for_ticker(
            ticker, session, lookback_days=news_lookback_days,
        )
        past_signals = _load_past_signals(ticker, session, limit=5)
        sector_rotation = compute_sector_rotation(session, lookback_days=5)

        return {
            "ticker_info": ticker_info,
            "technical_features": technical,
            "recent_news": news_items,
            "sector_rotation_5d": sector_rotation,
            "past_signals": past_signals,
        }


# --- Context loaders --------------------------------------------------------

def _load_ticker_info(ticker: str, session: Session) -> dict | None:
    """Fondamentaux Sika depuis la dernière quote en DB."""
    q = session.execute(
        select(Quote)
        .where(Quote.ticker == ticker)
        .order_by(Quote.quote_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not q:
        return None
    extras = q.extras or {}
    age_days = None
    if q.quote_date:
        age_days = (datetime.now(UTC) - q.quote_date).days
    return {
        "ticker": q.ticker,
        "name": q.name or "",
        "sector": q.sector or "",
        "country": q.country or "",
        "quote_date": q.quote_date.isoformat() if q.quote_date else None,
        "quote_age_days": age_days,
        "close_price": q.close_price,
        "previous_close": extras.get("previous_close"),
        "variation_pct": q.variation_pct,
        "volume_shares": q.volume,
        "value_traded": q.value_traded,
        "per": extras.get("per"),
        "dividend": extras.get("dividend"),
        "dividend_yield_pct": extras.get("dividend_yield_pct"),
        "market_cap_mfcfa": extras.get("market_cap_mfcfa"),
        "beta_1y": extras.get("beta_1y"),
        "rsi": extras.get("rsi"),
    }


def _load_recent_news_for_ticker(
    ticker: str,
    session: Session,
    *,
    lookback_days: int,
    limit: int = 25,
) -> list[dict]:
    """News enrichies mentionnant le ticker dans la fenêtre.

    Filtrage côté Python sur `tickers_mentioned` — le JSON est court (~10
    tickers max par article), le coût est négligeable et ça évite de dépendre
    d'un index GIN qui n'est pas garanti côté Postgres/Railway.
    """
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    rows = session.execute(
        select(NewsArticle)
        .where(NewsArticle.published_at >= cutoff)
        .where(NewsArticle.enriched_at.is_not(None))
        .order_by(NewsArticle.published_at.desc())
        .limit(500)
    ).scalars().all()

    out: list[dict] = []
    for art in rows:
        tickers = art.tickers_mentioned or []
        if ticker not in {str(t).strip().upper() for t in tickers}:
            continue
        out.append({
            "title": art.title,
            "url": art.url,
            "published_at": art.published_at.isoformat() if art.published_at else None,
            "source_key": art.source_key,
            "summary": art.summary or "",
            "enrichment": art.enrichment or {},
        })
        if len(out) >= limit:
            break
    return out


def _load_past_signals(
    ticker: str,
    session: Session,
    *,
    limit: int = 5,
) -> list[dict]:
    """Derniers signaux du pipeline quotidien sur ce ticker."""
    rows = session.execute(
        select(Signal, Brief.brief_date)
        .join(Brief, Signal.brief_id == Brief.id)
        .where(Signal.ticker == ticker)
        .order_by(Signal.signal_date.desc())
        .limit(limit)
    ).all()
    return [
        {
            "direction": sig.direction,
            "conviction": sig.conviction,
            "thesis": sig.thesis,
            "price_at_signal": sig.price_at_signal,
            "signal_date": sig.signal_date.isoformat() if sig.signal_date else None,
            "brief_date": brief_date.isoformat() if brief_date else None,
        }
        for sig, brief_date in rows
    ]


# --- Sanitization / anti-hallucination --------------------------------------

def _sanitize_prices(
    data: dict,
    *,
    current_price: float,
    horizon: str,
    model: str,
    ticker: str,
) -> dict:
    """Force `price_at_analysis` au close DB et borne `price_target` dans
    [0.5× ; 2.0×] du prix actuel. Trace Sentry si Opus avait hallucinée.

    Politique : **sanitize silencieusement plutôt que rejeter** — on garde la
    `recommendation` et la `rationale`, on corrige juste les chiffres aberrants.
    L'alerting Sentry permet de repérer les dérives récurrentes (→ refactor
    prompt).
    """
    if not isinstance(data, dict):
        return {"_error": "opus_returned_non_dict"}

    issues: list[str] = []

    # --- price_at_analysis : on force au close DB -----------------------------
    opus_price = _safe_float(data.get("price_at_analysis"))
    if opus_price is None or current_price <= 0:
        data["price_at_analysis"] = current_price
    else:
        delta_pct = abs(opus_price - current_price) / current_price * 100
        if delta_pct > _PRICE_SNAPSHOT_TOLERANCE_PCT:
            issues.append(
                f"price_at_analysis rectifié: opus={opus_price} → db={current_price}",
            )
        data["price_at_analysis"] = current_price

    # --- price_target : bornage [0.5× ; 2.0×] --------------------------------
    target = _safe_float(data.get("price_target"))
    if target is not None and current_price > 0:
        min_allowed = current_price * _PRICE_TARGET_MIN_RATIO
        max_allowed = current_price * _PRICE_TARGET_MAX_RATIO
        if target < min_allowed or target > max_allowed:
            issues.append(
                f"price_target hors bornes [{min_allowed:.0f}, {max_allowed:.0f}] "
                f"(opus={target}) → neutralisé",
            )
            # Politique : on neutralise (None) plutôt que clipper — un target
            # absurde signale qu'Opus a perdu son ancrage, vaut mieux rien
            # afficher qu'un chiffre faux.
            data["price_target"] = None

    # --- stop_loss : cohérence minimale avec recommendation ------------------
    recommendation = str(data.get("recommendation", "")).lower()
    stop = _safe_float(data.get("stop_loss"))
    if recommendation == "buy" and stop is not None and stop >= current_price:
        issues.append(
            f"stop_loss >= current ({stop} >= {current_price}) sur buy → neutralisé",
        )
        data["stop_loss"] = None

    if issues:
        logger.warning(
            f"InvestmentAdvisor sanitize ({ticker}/{horizon}): {issues}",
        )
        try:
            import sentry_sdk
        except ImportError:
            pass
        else:
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("component", "llm")
                scope.set_tag("step", "invest_decision")
                scope.set_tag("model", model)
                scope.set_tag("error_kind", "hallucinated_price")
                scope.set_tag("ticker", ticker)
                scope.set_tag("horizon", horizon)
                scope.set_context(
                    "sanitize_issues",
                    {"issues": issues, "current_price": current_price},
                )
                sentry_sdk.capture_message(
                    f"InvestmentAdvisor sanitize: {len(issues)} issue(s) on {ticker}",
                    level="warning",
                )
        data["_sanitize_issues"] = issues

    return data


# --- Coercion helpers -------------------------------------------------------

def _coerce_recommendation(value) -> str:
    """Normalise la recommandation en {buy, hold, avoid}. Fallback 'hold'."""
    if not isinstance(value, str):
        return "hold"
    v = value.strip().lower()
    # Synonymes charitables — Opus peut dériver malgré le prompt.
    if v in {"buy", "acheter", "achat"}:
        return "buy"
    if v in {"avoid", "sell", "éviter", "eviter", "vendre"}:
        return "avoid"
    return "hold"


def _clamp_confidence(value) -> float:
    """Confiance bornée dans [0.0, 1.0]. Fallback 0.0."""
    f = _safe_float(value)
    if f is None:
        return 0.0
    return max(0.0, min(1.0, f))


def _clamp_horizon_days(value, *, horizon: str) -> int | None:
    """Contraint time_horizon_days dans la fenêtre de l'horizon."""
    if value is None:
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    config = _HORIZON_CONFIG.get(horizon)
    if not config:
        return days
    return max(config["min_days"], min(config["max_days"], days))


def _safe_float(value) -> float | None:
    """Conversion float tolérante — None si impossible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Degraded result (fallback) ---------------------------------------------

def _degraded_result(
    *,
    reason: str,
    model: str,
    price_at_analysis: float,
    raw_preview: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> InvestmentAnalysisResult:
    """Résultat `hold` neutre quand Opus a échoué. Permet au router de répondre
    proprement 200 avec un flag `_error=True` dans le payload, au lieu de 500
    (mauvaise UX admin)."""
    return InvestmentAnalysisResult(
        recommendation="hold",
        confidence=0.0,
        price_at_analysis=price_at_analysis,
        price_target=None,
        stop_loss=None,
        time_horizon_days=None,
        payload={
            "recommendation": "hold",
            "confidence": 0.0,
            "price_at_analysis": price_at_analysis,
            "rationale": [f"Analyse indisponible : {reason[:200]}"],
            "risks": ["Données d'analyse non disponibles, ne pas agir sur cette sortie."],
            "_error": True,
            "_error_reason": reason,
            "_raw_preview": raw_preview,
        },
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_used=model,
    )
