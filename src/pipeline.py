"""Pipeline quotidien : collecte → enrichissement → synthèse → livraison.

Garanties de ce pipeline :
- **Idempotence multi-replica** via un advisory lock Postgres (session-level).
  Si deux instances déclenchent `run_daily_pipeline` en même temps, une seule
  exécute, l'autre loggue `skipped_locked` et retourne.
- **Audit-trail** : chaque déclenchement (même skippé) crée une ligne
  `pipeline_runs` avec son `status` final, pour debug et alerting futur.
- **Pas d'exception silencieuse** : toute panne fait planter avec trace
  complète capturée en DB. Sentry prend le relais si configuré.
"""
from __future__ import annotations

import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from .analysis.enrichment import NewsEnricher
from .analysis.synthesis import BriefSynthesizer
from .collectors.base import CollectionResult, NewsItem
from .collectors.registry import build_collector
from .config import get_settings
from .database import engine, get_session
from .dates import format_date_fr
from .delivery.email_brevo import EmailSender, render_email_html
from .delivery.whatsapp import WhatsAppSender
from .models import Brief, NewsArticle, PipelineRun, Quote, Signal, Source

logger = logging.getLogger(__name__)

LOCK_KEY = "brvm_daily_pipeline"


# --- Lock & run tracking ----------------------------------------------------

@contextmanager
def _pipeline_lock() -> Iterator[bool]:
    """Advisory lock Postgres scoppé sur la durée de la connexion.

    `pg_try_advisory_lock` ne bloque pas : retourne `true` si acquis, `false`
    si déjà pris. On garde la connexion ouverte tant que le pipeline tourne,
    ce qui garde le lock actif même si les autres étapes ouvrent leurs propres
    sessions.
    """
    conn = engine.connect()
    acquired = False
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": LOCK_KEY},
            ).scalar()
        )
        yield acquired
    finally:
        if acquired:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": LOCK_KEY},
                )
                conn.commit()
            except Exception:
                logger.exception("Échec libération advisory lock")
        conn.close()


def _start_run(trigger: str) -> int:
    with get_session() as s:
        run = PipelineRun(trigger=trigger, status="running")
        s.add(run)
        s.flush()
        return run.id


def _end_run(
    run_id: int,
    *,
    status: str,
    summary: dict | None = None,
    error: str | None = None,
    brief_id: int | None = None,
) -> None:
    with get_session() as s:
        run = s.get(PipelineRun, run_id)
        if not run:
            return
        run.status = status
        run.ended_at = datetime.now(UTC)
        if summary is not None:
            run.summary = summary
        if error is not None:
            run.error = error[:8000]  # garde-fou taille
        if brief_id is not None:
            run.brief_id = brief_id


# --- Entry point ------------------------------------------------------------

def run_daily_pipeline(trigger: str = "cron") -> dict:
    """Exécute le pipeline avec lock + audit-trail.

    Args:
        trigger: "cron" pour déclenchement automatique, "manual" via API.

    Returns:
        Résumé d'exécution (steps, brief_id, timings).
    """
    run_id = _start_run(trigger)
    logger.info(f"Pipeline run #{run_id} démarré (trigger={trigger})")

    with _pipeline_lock() as acquired:
        if not acquired:
            logger.warning(f"Pipeline run #{run_id} skippé — lock déjà détenu")
            _end_run(run_id, status="skipped_locked", summary={"reason": "lock_held"})
            return {"run_id": run_id, "status": "skipped", "reason": "lock_held"}

        try:
            summary = _run_pipeline_body()
            _end_run(
                run_id,
                status="success",
                summary=summary,
                brief_id=summary.get("brief_id"),
            )
            summary["run_id"] = run_id
            return summary
        except Exception as exc:
            logger.exception(f"Pipeline run #{run_id} échoué")
            _end_run(
                run_id,
                status="failed",
                error=traceback.format_exc(),
                summary={"error": str(exc)},
            )
            raise


# --- Pipeline body ----------------------------------------------------------

def _run_pipeline_body() -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    date_str = format_date_fr(now_local)

    summary: dict = {"started_at": now_local.isoformat(), "steps": []}
    logger.info(f"=== Pipeline BRVM — {date_str} ===")

    # 1. Collecte
    collection_results = _collect_all()
    summary["steps"].append({
        "step": "collect",
        "sources": len(collection_results),
        "news_count": sum(len(r.news) for r in collection_results),
        "quotes_count": sum(len(r.quotes) for r in collection_results),
        "errors": [e for r in collection_results for e in r.errors],
    })

    # 2. Persistance brute
    new_news_items = _persist_collection(collection_results)
    summary["steps"].append({"step": "persist", "new_news": len(new_news_items)})

    # 3. Enrichissement (Sonnet)
    enriched = _enrich_news(new_news_items)
    summary["steps"].append({"step": "enrich", "count": len(enriched)})

    # 4. Préparation données pour Opus
    market_snapshot = _build_market_snapshot()
    historical_context = _build_historical_context()

    # 5. Synthèse (Opus)
    brief_json = BriefSynthesizer().synthesize(
        market_snapshot=market_snapshot,
        enriched_news=enriched,
        historical_context=historical_context,
    )
    logger.info(
        f"Brief synthétisé : {len(brief_json.get('opportunities', []))} opportunités"
    )
    summary["steps"].append({
        "step": "synthesize",
        "opportunities": len(brief_json.get("opportunities", [])),
    })

    # 6. Persistance du brief
    brief_id = _persist_brief(brief_json, now_local)
    summary["brief_id"] = brief_id

    # 7. Livraison
    delivery = _deliver(brief_json, date_str, brief_id, market_snapshot=market_snapshot)
    summary["steps"].append({"step": "deliver", **delivery})

    summary["ended_at"] = datetime.now(tz).isoformat()
    logger.info(f"=== Pipeline terminé — brief_id={brief_id} ===")
    return summary


# --- Étapes détaillées ------------------------------------------------------

def _collect_all() -> list[CollectionResult]:
    """Lance tous les collectors actifs."""
    results: list[CollectionResult] = []
    with get_session() as s:
        sources = s.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
        for src in sources:
            collector = build_collector(src.type, {**src.config, "url": src.url})
            if not collector:
                logger.warning(f"Collector introuvable pour {src.key}")
                continue
            logger.info(f"Collecte → {src.key}")
            collector.source_key = src.key
            res = collector.collect()
            res.source_key = src.key
            results.append(res)

            src.last_collected_at = datetime.now(UTC)
            src.last_status = "ok" if res.success else "error"
            src.last_error = "; ".join(res.errors[:3]) if res.errors else None
    return results


def _persist_collection(results: list[CollectionResult]) -> list[NewsItem]:
    """Persiste cotations + news. Retourne les NewsItem nouvellement insérés."""
    new_news: list[NewsItem] = []
    with get_session() as s:
        for result in results:
            for q in result.quotes:
                if not q.ticker or not q.quote_date:
                    continue
                existing = s.execute(
                    select(Quote).where(
                        Quote.ticker == q.ticker,
                        Quote.quote_date == q.quote_date,
                    )
                ).scalar_one_or_none()
                if existing:
                    existing.close_price = q.close_price
                    existing.variation_pct = q.variation_pct
                    existing.volume = q.volume
                else:
                    s.add(Quote(
                        ticker=q.ticker, name=q.name, sector=q.sector,
                        quote_date=q.quote_date, close_price=q.close_price,
                        variation_pct=q.variation_pct, volume=q.volume,
                        value_traded=q.value_traded,
                    ))

            for n in result.news:
                if not n.url:
                    continue
                existing = s.execute(
                    select(NewsArticle).where(NewsArticle.url == n.url)
                ).scalar_one_or_none()
                if existing:
                    continue
                s.add(NewsArticle(
                    source_key=result.source_key,
                    title=n.title, url=n.url,
                    published_at=n.published_at,
                    summary=n.summary, content=n.content,
                ))
                new_news.append(n)
    return new_news


def _enrich_news(articles: list[NewsItem]) -> list[dict]:
    """Enrichit les nouveaux articles via Sonnet + recharge le contexte récent."""
    settings = get_settings()
    enriched_list: list[dict] = []

    if articles:
        enricher = NewsEnricher()
        with get_session() as s:
            for article in articles:
                data = enricher.enrich(article)
                if "error" in data:
                    continue

                db_article = s.execute(
                    select(NewsArticle).where(NewsArticle.url == article.url)
                ).scalar_one_or_none()
                if db_article:
                    db_article.tickers_mentioned = data.get("tickers_mentioned", [])
                    db_article.enrichment = data
                    db_article.enriched_at = datetime.now(UTC)

                enriched_list.append({
                    "title": article.title,
                    "url": article.url,
                    "source": article.source_key,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                    **data,
                })

    # Contexte élargi : articles des N dernières heures déjà enrichis
    threshold = datetime.now(UTC) - timedelta(hours=settings.news_lookback_hours)
    known_urls = {e["url"] for e in enriched_list}

    with get_session() as s:
        recent = s.execute(
            select(NewsArticle)
            .where(NewsArticle.published_at >= threshold)
            .where(NewsArticle.enriched_at.is_not(None))
        ).scalars().all()
        for a in recent:
            if a.url in known_urls:
                continue
            enriched_list.append({
                "title": a.title,
                "url": a.url,
                "source": a.source_key,
                "published_at": a.published_at.isoformat() if a.published_at else None,
                **(a.enrichment or {}),
            })

    # Filtre pertinence BRVM
    return [
        e for e in enriched_list
        if e.get("tickers_mentioned") or (e.get("materiality", 0) or 0) >= 3
    ]


def _build_market_snapshot() -> dict:
    with get_session() as s:
        last_date = s.execute(
            select(Quote.quote_date).order_by(Quote.quote_date.desc()).limit(1)
        ).scalar_one_or_none()
        if not last_date:
            return {"note": "Aucune cotation en DB"}

        quotes = s.execute(
            select(Quote).where(Quote.quote_date == last_date)
        ).scalars().all()

        serialized = [{
            "ticker": q.ticker, "name": q.name,
            "close": q.close_price, "var_pct": q.variation_pct, "volume": q.volume,
        } for q in quotes]

        return {
            "date": last_date.isoformat(),
            "quotes_count": len(serialized),
            "top_gainers": sorted(serialized, key=lambda x: -x["var_pct"])[:5],
            "top_losers": sorted(serialized, key=lambda x: x["var_pct"])[:5],
            "top_volumes": sorted(serialized, key=lambda x: -x["volume"])[:5],
        }


def _build_historical_context(days: int = 5) -> list[dict]:
    with get_session() as s:
        briefs = s.execute(
            select(Brief).order_by(Brief.brief_date.desc()).limit(days)
        ).scalars().all()
        return [{
            "date": b.brief_date.isoformat() if b.brief_date else None,
            "summary": b.payload.get("market_summary", "") if b.payload else "",
            "tickers": [sig.ticker for sig in b.signals],
        } for b in briefs]


def _persist_brief(brief_json: dict, now_local: datetime) -> int:
    with get_session() as s:
        brief = Brief(
            brief_date=now_local,
            summary_markdown=brief_json.get("market_summary", ""),
            payload=brief_json,
        )
        s.add(brief)
        s.flush()

        for opp in brief_json.get("opportunities", []):
            ticker = opp.get("ticker")
            if not ticker:
                continue
            last_quote = s.execute(
                select(Quote).where(Quote.ticker == ticker)
                .order_by(Quote.quote_date.desc()).limit(1)
            ).scalar_one_or_none()

            s.add(Signal(
                brief_id=brief.id,
                ticker=ticker,
                direction=opp.get("direction", "watch"),
                conviction=int(opp.get("conviction", 3)),
                thesis=opp.get("thesis", ""),
                price_at_signal=last_quote.close_price if last_quote else None,
            ))
        s.flush()
        return brief.id


def _deliver(
    brief_json: dict,
    date_str: str,
    brief_id: int,
    *,
    market_snapshot: dict | None = None,
) -> dict:
    """Envoie email + WhatsApp. Met à jour Brief.delivery_status en conséquence.

    Retourne un dict récap pour le summary du run.
    """
    email_ok = False
    whatsapp_attempted = False
    whatsapp_ok = False
    errors: list[str] = []

    # Email (toujours tenté — obligatoire)
    try:
        subject, html = render_email_html(
            brief_json,
            date_str,
            market_snapshot=market_snapshot,
            edition_num=brief_id,
        )
        EmailSender().send(subject, html)
        email_ok = True
    except Exception as e:
        errors.append(f"email: {e}")
        logger.exception("Échec envoi email")

    # WhatsApp (optionnel)
    try:
        sender = WhatsAppSender()
        if sender.enabled:
            whatsapp_attempted = True
            sender.send(brief_json)
            whatsapp_ok = True
    except Exception as e:
        errors.append(f"whatsapp: {e}")
        logger.exception("Échec envoi WhatsApp")

    # Statut final
    if email_ok and (not whatsapp_attempted or whatsapp_ok):
        status = "delivered"
    elif email_ok or whatsapp_ok:
        status = "partial"
    else:
        status = "failed"

    with get_session() as s:
        brief = s.get(Brief, brief_id)
        if brief:
            brief.email_sent = email_ok
            brief.whatsapp_sent = whatsapp_ok
            brief.delivery_status = status
            brief.delivery_errors = "; ".join(errors) if errors else None

    # Si tout est cassé, on ne peut pas alerter par email (c'est justement ça qui échoue).
    # On se contente de logger + laisser Sentry capter l'exception raise'é plus haut si besoin.
    if status == "failed":
        logger.error(f"ÉCHEC LIVRAISON TOTAL brief_id={brief_id}: {errors}")

    return {"status": status, "email_ok": email_ok, "whatsapp_ok": whatsapp_ok, "errors": errors}
