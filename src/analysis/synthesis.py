"""Synthèse finale du brief matinal par Claude Opus."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import Anthropic, APIError

from ..config import get_settings
from ._retry import anthropic_retry
from .enrichment import _strip_fence

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "synthesis.md"


class BriefSynthesizer:
    def __init__(self):
        self.settings = get_settings()
        self.client = Anthropic(api_key=self.settings.anthropic_api_key)
        template = PROMPT_PATH.read_text(encoding="utf-8")
        self.system_prompt = template.replace("{{investor_profile}}", self.settings.investor_profile)

    @anthropic_retry
    def _call_llm(self, user_content: str) -> str:
        resp = self.client.messages.create(
            model=self.settings.model_synthesis,
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
    ) -> dict:
        """Appelle Opus pour produire le brief JSON final. Retourne toujours un dict valide."""
        payload = {
            "market_snapshot": market_snapshot,
            "enriched_news": enriched_news,
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
            return _error_payload(f"anthropic: {e}")
        except Exception as e:
            logger.exception("Brief synthesis failed (unknown)")
            return _error_payload(str(e))

        try:
            return json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            logger.error(f"Brief synthesis JSON invalide: {e} — raw[:300]={raw[:300]!r}")
            return _error_payload(f"JSON parse error: {e}", raw_preview=raw[:500])


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
