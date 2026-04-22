"""Synthèse hebdomadaire — audit 7j des recommandations BRVM.

Produit un brief d'audit pour un comité de pilotage (expert, sponsor,
conseiller senior). Philosophiquement distinct du brief daily : pas de
nouveaux signaux, uniquement une revue de performance + outlook semaine à
venir. Même moteur (Opus), prompt différent (`prompts/weekly_synthesis.md`).

Le prompt reçoit tout le contexte déjà préparé : briefs daily de la semaine +
plays enrichis du P&L réel (calculé côté backend, pas par le LLM). Le LLM ne
fait que classifier won/lost/pending + rédiger les leçons et le narratif.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import Anthropic, APIError

from ..config import get_settings
from ._retry import anthropic_retry
from .enrichment import _capture_llm_error, _strip_fence

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "weekly_synthesis.md"


class WeeklyBriefSynthesizer:
    """Appelle Opus pour produire le brief hebdo d'audit."""

    def __init__(self, model: str | None = None):
        self.settings = get_settings()
        self.client = Anthropic(api_key=self.settings.anthropic_api_key)
        self.model = model or self.settings.model_synthesis
        template = PROMPT_PATH.read_text(encoding="utf-8")
        self.system_prompt = template.replace(
            "{{investor_profile}}", self.settings.investor_profile,
        )

    @anthropic_retry
    def _call_llm(self, user_content: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            # Le weekly est plus volumineux qu'un daily (scorecard + N plays + narratif)
            # → on double le budget tokens pour éviter les troncatures mid-JSON.
            max_tokens=6000,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return resp.content[0].text

    def synthesize(
        self,
        *,
        week_start: str,
        week_end: str,
        daily_briefs: list[dict],
        plays_with_pnl: list[dict],
        week_quotes: list[dict],
        week_news: list[dict],
        week_ahead_catalysts: list[str] | None = None,
        user_trades: dict | None = None,
    ) -> dict:
        """Appelle Opus pour produire le brief hebdo. Retourne toujours un dict.

        Args:
            week_start / week_end : bornes ISO YYYY-MM-DD de la fenêtre.
            daily_briefs : liste de {brief_id, brief_date, market_summary,
                market_regime, opportunities} des briefs daily de la semaine.
            plays_with_pnl : liste enrichie avec `realized_pnl_pct` déjà
                calculé et `days_held`. Le LLM ne recalcule pas — il classifie.
            week_quotes : mouvement de la semaine par ticker (open/close/volume).
            week_news : news structurelles déjà filtrées en amont.
            week_ahead_catalysts : catalyseurs connus pour la semaine suivante.
            user_trades : `{trades: [...], stats: {...}}` — trades auto-reportés
                par l'utilisateur dans la fenêtre, avec P&L mark-to-market et
                attribution signal vs intuition.
        """
        payload = {
            "week_start": week_start,
            "week_end": week_end,
            "daily_briefs": daily_briefs,
            "plays_with_pnl": plays_with_pnl,
            "week_quotes": week_quotes,
            "week_news": week_news,
            "week_ahead": week_ahead_catalysts or [],
            "user_trades": user_trades or {"trades": [], "stats": {"total": 0}},
        }
        user_content = (
            "Voici les données de la semaine. Produis le JSON d'audit "
            "conformément au schéma.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}"
        )

        raw = ""
        try:
            raw = self._call_llm(user_content)
        except APIError as e:
            logger.exception("Weekly brief synthesis failed (Anthropic)")
            _capture_llm_error(
                e, step="weekly_synthesize", model=self.model,
                error_kind="anthropic_api",
            )
            return _weekly_error_payload(
                week_start, week_end, f"anthropic: {e}",
            )
        except Exception as e:
            logger.exception("Weekly brief synthesis failed (unknown)")
            _capture_llm_error(
                e, step="weekly_synthesize", model=self.model,
                error_kind="unknown",
            )
            return _weekly_error_payload(week_start, week_end, str(e))

        try:
            data = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            logger.error(
                f"Weekly brief JSON invalide: {e} — raw[:300]={raw[:300]!r}"
            )
            _capture_llm_error(
                e, step="weekly_synthesize", model=self.model,
                error_kind="invalid_json",
            )
            return _weekly_error_payload(
                week_start, week_end, f"JSON parse error: {e}",
                raw_preview=raw[:500],
            )

        # Cohérence : le LLM peut se tromper sur des totaux ou re-calculer des
        # P&L. On force les valeurs issues de `plays_with_pnl` (source de vérité
        # backend) pour chaque play retourné, sans toucher au classement outcome
        # ni à la `lesson` (c'est son rôle d'analyste).
        data = _reconcile_pnl(data, plays_with_pnl)
        data = _reconcile_scorecard(data)
        # trade_execution : on garde le commentary du LLM mais on remplace
        # les stats numériques par celles calculées côté backend.
        data = _reconcile_trade_execution(data, user_trades)

        # Force les bornes demandées — le LLM peut les copier mal.
        data["week_start"] = week_start
        data["week_end"] = week_end
        return data


# --- Reconciliation ---------------------------------------------------------

def _reconcile_pnl(data: dict, plays_with_pnl: list[dict]) -> dict:
    """Remplace `price_at_signal`, `current_price`, `realized_pnl_pct` par les
    valeurs canoniques (backend). Le LLM peut arrondir ou se tromper ; ces
    champs sont chiffrés, ils doivent être fiables.

    On matche sur `(ticker, issued_on)` pour gérer un même ticker émis
    plusieurs fois dans la semaine.
    """
    plays = data.get("plays") or []
    if not isinstance(plays, list):
        return data

    by_key: dict[tuple[str, str], dict] = {}
    for p in plays_with_pnl:
        if not isinstance(p, dict):
            continue
        key = (
            str(p.get("ticker") or "").upper(),
            str(p.get("issued_on") or ""),
        )
        by_key[key] = p

    reconciled: list[dict] = []
    for play in plays:
        if not isinstance(play, dict):
            reconciled.append(play)
            continue
        key = (
            str(play.get("ticker") or "").upper(),
            str(play.get("issued_on") or ""),
        )
        src = by_key.get(key)
        if src is not None:
            play = {
                **play,
                "price_at_signal": src.get("price_at_signal"),
                "current_price": src.get("current_price"),
                "realized_pnl_pct": src.get("realized_pnl_pct"),
            }
        reconciled.append(play)
    data["plays"] = reconciled
    return data


def _reconcile_scorecard(data: dict) -> dict:
    """Recalcule le scorecard depuis les plays — le LLM peut se tromper sur
    la somme. On ne touche pas au `market_regime` ni au `week_summary`.

    Règle cohérente avec le prompt :
      - `total_calls` = plays avec direction in {buy, avoid}
      - wins/losses/pending calculés depuis `outcome`
      - `avg_realized_pnl_pct` sur won+lost uniquement
    """
    plays = data.get("plays") or []
    if not isinstance(plays, list):
        return data

    actionable = [
        p for p in plays
        if isinstance(p, dict) and (p.get("direction") in ("buy", "avoid"))
    ]
    wins = sum(1 for p in actionable if p.get("outcome") == "won")
    losses = sum(1 for p in actionable if p.get("outcome") == "lost")
    pending = sum(1 for p in actionable if p.get("outcome") == "pending")

    closed = [
        p for p in actionable
        if p.get("outcome") in ("won", "lost")
        and isinstance(p.get("realized_pnl_pct"), (int, float))
    ]
    avg_pnl = None
    best = None
    worst = None
    if closed:
        pnls = [float(p["realized_pnl_pct"]) for p in closed]
        avg_pnl = round(sum(pnls) / len(pnls), 2)
        best_p = max(closed, key=lambda p: float(p["realized_pnl_pct"]))
        worst_p = min(closed, key=lambda p: float(p["realized_pnl_pct"]))
        best = {
            "ticker": best_p.get("ticker"),
            "pnl_pct": round(float(best_p["realized_pnl_pct"]), 2),
        }
        worst = {
            "ticker": worst_p.get("ticker"),
            "pnl_pct": round(float(worst_p["realized_pnl_pct"]), 2),
        }

    data["scorecard"] = {
        "total_calls": len(actionable),
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "avg_realized_pnl_pct": avg_pnl,
        "best_ticker": best["ticker"] if best else None,
        "best_pnl_pct": best["pnl_pct"] if best else None,
        "worst_ticker": worst["ticker"] if worst else None,
        "worst_pnl_pct": worst["pnl_pct"] if worst else None,
    }
    return data


def _reconcile_trade_execution(data: dict, user_trades: dict | None) -> dict:
    """Écrase les stats de `trade_execution` avec celles du backend.

    Le LLM peut renseigner `commentary` (narratif) mais pas les totaux. Si
    l'utilisateur n'a déclaré aucun trade (`user_trades.stats.total == 0`),
    on remonte un objet vide → le template email n'affichera pas la section.
    """
    stats = (user_trades or {}).get("stats") or {}
    existing = data.get("trade_execution") or {}
    commentary = (
        existing.get("commentary", "")
        if isinstance(existing, dict) else ""
    )
    data["trade_execution"] = {
        "total_trades": int(stats.get("total") or 0),
        "following_signal": int(stats.get("following_signal") or 0),
        "autonomous": int(stats.get("autonomous") or 0),
        "avg_unrealized_pnl_pct": stats.get("avg_unrealized_pnl_pct"),
        "commentary": commentary,
    }
    return data


def _weekly_error_payload(
    week_start: str, week_end: str, reason: str, raw_preview: str = "",
) -> dict:
    """Payload stub quand Opus échoue — permet de persister un brief d'alerte
    plutôt que de crasher le pipeline weekly."""
    return {
        "week_start": week_start,
        "week_end": week_end,
        "market_regime": None,
        "week_summary": "Erreur de génération du brief hebdomadaire. Voir logs.",
        "scorecard": {
            "total_calls": 0, "wins": 0, "losses": 0, "pending": 0,
            "avg_realized_pnl_pct": None,
            "best_ticker": None, "best_pnl_pct": None,
            "worst_ticker": None, "worst_pnl_pct": None,
        },
        "plays": [],
        "structural_news": [f"Synthèse indisponible : {reason[:200]}"],
        "week_ahead_catalysts": [],
        "watchlist_updates": [],
        "_error": True,
        "_raw_preview": raw_preview,
    }
