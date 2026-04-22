"""Enrichissement des news par Claude Sonnet.

Pour chaque article on extrait : tickers mentionnés, sentiment, matérialité,
résumé. Retourne toujours un dict (jamais d'exception) — en cas d'erreur
irrécupérable après retries, on retourne `{"error": ...}` pour que le pipeline
puisse skipper l'article sans tout casser.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import Anthropic, APIError

from ..collectors.base import NewsItem
from ..config import get_settings
from ._retry import anthropic_retry

logger = logging.getLogger(__name__)


def _capture_llm_error(exc: BaseException, *, step: str, model: str, **tags: str) -> None:
    """Capture une erreur LLM dans Sentry avec tags structurés (C-4).

    Silencieux si Sentry n'est pas initialisé — évite tout couplage obligatoire.
    Les tags permettent ensuite de filtrer dans Sentry : `step:enrich`,
    `model:claude-sonnet-4-6`, etc.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", "llm")
        scope.set_tag("step", step)
        scope.set_tag("model", model)
        for k, v in tags.items():
            scope.set_tag(k, v)
        sentry_sdk.capture_exception(exc)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "enrichment.md"


def _strip_fence(raw: str) -> str:
    """Retire les fences ```json ... ``` si le modèle en ajoute."""
    raw = raw.strip()
    if not raw.startswith("```"):
        return raw
    body = raw.split("```", 2)
    if len(body) < 2:
        return raw
    inner = body[1]
    if inner.startswith("json"):
        inner = inner[4:]
    return inner.strip()


class NewsEnricher:
    def __init__(self):
        self.settings = get_settings()
        self.client = Anthropic(api_key=self.settings.anthropic_api_key)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    @anthropic_retry
    def _call_llm(self, user_content: str) -> str:
        resp = self.client.messages.create(
            model=self.settings.model_enrichment,
            max_tokens=800,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return resp.content[0].text

    def enrich(self, article: NewsItem) -> dict:
        """Renvoie le dict enrichi. Jamais d'exception — `{"error": ...}` en dernier recours."""
        user_content = (
            f"TITRE: {article.title}\n"
            f"URL: {article.url}\n"
            f"SOURCE: {article.source_key}\n"
            f"DATE: {article.published_at.isoformat() if article.published_at else 'inconnue'}\n\n"
            f"RÉSUMÉ: {article.summary or (article.content or '')[:2000]}"
        )

        raw = ""
        try:
            raw = self._call_llm(user_content)
        except APIError as e:
            logger.error(f"Enrichissement Anthropic échoué '{article.title[:60]}': {e}")
            _capture_llm_error(
                e, step="enrich", model=self.settings.model_enrichment,
                error_kind="anthropic_api", article_url=article.url[:180],
            )
            return {"error": f"anthropic: {e}"}
        except Exception as e:
            logger.exception(f"Enrichissement inconnu échoué '{article.title[:60]}'")
            _capture_llm_error(
                e, step="enrich", model=self.settings.model_enrichment,
                error_kind="unknown", article_url=article.url[:180],
            )
            return {"error": str(e)}

        try:
            return json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            logger.warning(f"JSON invalide pour '{article.title[:60]}': {e}")
            _capture_llm_error(
                e, step="enrich", model=self.settings.model_enrichment,
                error_kind="invalid_json", article_url=article.url[:180],
            )
            return {"error": "invalid_json", "raw": raw[:500]}

    def enrich_batch(self, articles: list[NewsItem]) -> list[dict]:
        """Enrichit une liste d'articles. Retourne la liste alignée des enrichments."""
        results = []
        for i, art in enumerate(articles):
            logger.info(f"Enrichissement {i + 1}/{len(articles)}: {art.title[:60]}")
            results.append(self.enrich(art))
        return results
