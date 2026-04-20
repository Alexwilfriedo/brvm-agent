"""Endpoints statistiques agrégées — KPIs dashboard (coût, activité).

Coût Anthropic : calcul approximatif basé sur le nombre de briefs produits × un
coût unitaire `$0.80/brief` (moyenne observée Opus 4.7 avec ~2.5k in tokens +
1.5k out tokens). Pour un calcul précis il faudrait traquer les tokens par run
(TODO : ajouter `cost_usd` dans `PipelineRun.summary`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from ..database import get_session
from ..models import Brief, MarketAnalysis, NewsArticle, PipelineRun
from .deps import require_admin

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_admin)])

# Coût unitaire par brief Opus — moyenne observée en production. À raffiner
# quand on traquera les tokens par run.
COST_PER_BRIEF_USD = 0.80


@router.get("/activity")
def activity_summary(
    days: int = Query(7, ge=1, le=90, description="Fenêtre de calcul en jours"),
):
    """KPIs compacts pour la barre de statut du dashboard.

    Retour :
      {
        period_days,
        briefs_count,                # briefs délivrés sur la période
        runs_count,                  # runs totaux
        runs_failed_count,           # runs en échec
        news_enriched_count,         # articles enrichis par Sonnet
        market_analyses_count,       # analyses marché générées
        estimated_cost_usd,          # coût Anthropic estimé
      }
    """
    threshold = datetime.now(UTC) - timedelta(days=days)

    with get_session() as s:
        briefs = s.execute(
            select(func.count(Brief.id)).where(Brief.created_at >= threshold)
        ).scalar_one()

        runs_total = s.execute(
            select(func.count(PipelineRun.id)).where(PipelineRun.started_at >= threshold)
        ).scalar_one()

        runs_failed = s.execute(
            select(func.count(PipelineRun.id))
            .where(PipelineRun.started_at >= threshold)
            .where(PipelineRun.status == "failed")
        ).scalar_one()

        news_enriched = s.execute(
            select(func.count(NewsArticle.id))
            .where(NewsArticle.enriched_at >= threshold)
        ).scalar_one()

        market_analyses = s.execute(
            select(func.count(MarketAnalysis.id))
            .where(MarketAnalysis.generated_at >= threshold)
        ).scalar_one()

        return {
            "period_days": days,
            "briefs_count": briefs,
            "runs_count": runs_total,
            "runs_failed_count": runs_failed,
            "news_enriched_count": news_enriched,
            "market_analyses_count": market_analyses,
            "estimated_cost_usd": round(briefs * COST_PER_BRIEF_USD, 2),
        }
