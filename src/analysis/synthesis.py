"""Synthèse finale du brief matinal par Claude Opus."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import Anthropic, APIError

from ..config import get_settings
from ._retry import anthropic_retry
from .enrichment import _capture_llm_error, _strip_fence

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "synthesis.md"


class BriefSynthesizer:
    def __init__(self, model: str | None = None):
        """`model` : override explicite (A/B test Q-1). Par défaut, utilise
        `settings.model_synthesis`."""
        self.settings = get_settings()
        self.client = Anthropic(api_key=self.settings.anthropic_api_key)
        self.model = model or self.settings.model_synthesis
        template = PROMPT_PATH.read_text(encoding="utf-8")
        self.system_prompt = template.replace("{{investor_profile}}", self.settings.investor_profile)

    @anthropic_retry
    def _call_llm(self, user_content: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4000,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return resp.content[0].text

    def synthesize(
        self,
        market_snapshot: dict,
        enriched_news: list[dict],
        historical_context: list[dict] | None = None,
        ticker_fundamentals: list[dict] | None = None,
    ) -> dict:
        """Appelle Opus pour produire le brief JSON final. Retourne toujours un dict valide.

        `ticker_fundamentals` : liste de dicts avec close_price + extras
        (per, dividend, dividend_yield, etc.) pour chaque ticker candidat.
        Permet à Opus de chiffrer price_current / target / valuation sans
        inventer. Voir `_build_ticker_fundamentals` dans pipeline.py.
        """
        payload = {
            "market_snapshot": market_snapshot,
            "enriched_news": enriched_news,
            "ticker_fundamentals": ticker_fundamentals or [],
            "historical_context": historical_context or [],
        }
        user_content = (
            "Voici les données d'entrée pour le brief d'aujourd'hui. "
            "Réponds avec le JSON strict demandé.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}"
        )

        raw = ""
        try:
            raw = self._call_llm(user_content)
        except APIError as e:
            logger.exception("Brief synthesis failed (Anthropic)")
            _capture_llm_error(
                e, step="synthesize", model=self.model,
                error_kind="anthropic_api",
            )
            return _error_payload(f"anthropic: {e}")
        except Exception as e:
            logger.exception("Brief synthesis failed (unknown)")
            _capture_llm_error(
                e, step="synthesize", model=self.model,
                error_kind="unknown",
            )
            return _error_payload(str(e))

        try:
            data = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            logger.error(f"Brief synthesis JSON invalide: {e} — raw[:300]={raw[:300]!r}")
            _capture_llm_error(
                e, step="synthesize", model=self.model,
                error_kind="invalid_json",
            )
            return _error_payload(f"JSON parse error: {e}", raw_preview=raw[:500])

        # A-5 : filtre anti-hallucination des opportunities
        data = _filter_hallucinated_tickers(
            data,
            market_snapshot=market_snapshot,
            enriched_news=enriched_news,
            ticker_fundamentals=ticker_fundamentals or [],
            model=self.model,
        )

        # Filet de sécurité : calculer gain_potential_pct si Opus l'a omis
        # alors qu'on a price_current + price_target.
        for opp in data.get("opportunities") or []:
            if not isinstance(opp, dict):
                continue
            if opp.get("gain_potential_pct") is None:
                cur = opp.get("price_current")
                tgt = opp.get("price_target")
                if isinstance(cur, (int, float)) and isinstance(tgt, (int, float)) and cur:
                    opp["gain_potential_pct"] = round((tgt - cur) / cur * 100, 2)
        return data


def _collect_input_tickers(
    market_snapshot: dict,
    enriched_news: list[dict],
    ticker_fundamentals: list[dict],
) -> set[str]:
    """Extrait l'ensemble des tickers "connus" depuis les données d'entrée.

    Un ticker présent dans cet ensemble est considéré comme légitime pour
    apparaître en `opportunities[]`. Tout ticker hors set = hallucination
    probable (Opus invente un code à partir du nom d'entreprise, d'une
    confusion avec un autre marché, etc.).
    """
    known: set[str] = set()

    def _add(v: str | None) -> None:
        if isinstance(v, str) and v.strip():
            known.add(v.strip().upper())

    # 1. Snapshot marché (gainers/losers/volumes)
    for bucket in ("top_gainers", "top_losers", "top_volumes"):
        for row in market_snapshot.get(bucket, []) or []:
            if isinstance(row, dict):
                _add(row.get("ticker"))

    # 2. Tickers mentionnés dans les news enrichies
    for art in enriched_news or []:
        if not isinstance(art, dict):
            continue
        for t in art.get("tickers_mentioned", []) or []:
            _add(t)
        enr = art.get("enrichment") or {}
        if isinstance(enr, dict):
            for t in enr.get("tickers_mentioned", []) or []:
                _add(t)

    # 3. Fundamentals fournis explicitement
    for f in ticker_fundamentals or []:
        if isinstance(f, dict):
            _add(f.get("ticker"))

    return known


def _filter_hallucinated_tickers(
    data: dict,
    *,
    market_snapshot: dict,
    enriched_news: list[dict],
    ticker_fundamentals: list[dict],
    model: str,
) -> dict:
    """A-5 : retire les opportunities dont le ticker n'apparaît dans aucune
    donnée d'entrée. Log WARNING + capture Sentry avec tag `hallucinated_ticker`.

    Politique : **filtrer silencieusement plutôt que planter** — un brief
    amputé d'un signal louche reste utile. La capture Sentry assure qu'on ne
    rate pas les cas fréquents (→ refactor prompt).
    """
    opps = data.get("opportunities") or []
    if not opps:
        return data

    known = _collect_input_tickers(market_snapshot, enriched_news, ticker_fundamentals)
    if not known:
        # Aucune source de vérité disponible : skip la validation pour ne pas
        # tout filtrer bêtement (cas boot initial, 0 quote en DB).
        return data

    kept: list = []
    dropped: list[str] = []
    for opp in opps:
        if not isinstance(opp, dict):
            kept.append(opp)
            continue
        ticker = (opp.get("ticker") or "").strip().upper()
        if not ticker or ticker in known:
            kept.append(opp)
        else:
            dropped.append(ticker)

    if dropped:
        logger.warning(
            f"A-5 hallucination filter: {len(dropped)} opportunity(ies) retirée(s) "
            f"(tickers absents du marché snapshot) : {dropped}"
        )
        # Capture Sentry à titre d'exception dédiée (pour alerting + dashboard)
        try:
            import sentry_sdk
        except ImportError:
            pass
        else:
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("component", "llm")
                scope.set_tag("step", "synthesize")
                scope.set_tag("model", model)
                scope.set_tag("error_kind", "hallucinated_ticker")
                scope.set_context("dropped_tickers", {"tickers": dropped})
                sentry_sdk.capture_message(
                    f"Hallucination filter dropped {len(dropped)} ticker(s)",
                    level="warning",
                )

        data = {**data, "opportunities": kept}
        # Trace dans le payload — visible côté admin UI pour debug + audit
        data["_hallucination_filter"] = {
            "dropped": dropped,
            "dropped_count": len(dropped),
        }
    return data


def _error_payload(reason: str, raw_preview: str = "") -> dict:
    """Payload minimal quand la synthèse échoue — permet au pipeline de continuer
    (persistance + livraison d'un brief d'alerte) au lieu de crasher."""
    return {
        "market_summary": "Erreur de génération du brief. Voir logs pour détail.",
        "opportunities": [],
        "alerts": [f"Synthèse indisponible : {reason[:200]}"],
        "skip_reasons": reason,
        "_error": True,
        "_raw_preview": raw_preview,
    }
