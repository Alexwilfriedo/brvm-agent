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

from . import events
from .analysis.enrichment import NewsEnricher
from .analysis.features import compute_sector_rotation, compute_technical_features
from .analysis.synthesis import BriefSynthesizer
from .analysis.weekly_synthesis import WeeklyBriefSynthesizer
from .collectors.base import CollectionResult, NewsItem
from .collectors.registry import build_collector
from .config import get_settings
from .database import engine, get_session
from .dates import format_date_fr
from .delivery.email_brevo import EmailSender, render_email_html
from .delivery.whatsapp import WhatsAppSender
from .models import Brief, NewsArticle, PipelineRun, Quote, Signal, Source, Trade

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


def _start_run(trigger: str, pipeline_type: str = "daily") -> int:
    with get_session() as s:
        run = PipelineRun(
            trigger=trigger, status="running", pipeline_type=pipeline_type,
        )
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

def run_daily_pipeline(trigger: str = "cron", force: bool = False) -> dict:
    """Exécute le pipeline avec lock + audit-trail + streaming SSE.

    Args:
        trigger: "cron" pour déclenchement automatique, "manual" via API.
        force: True pour bypasser l'idempotence-par-date. Si False et un brief
               existe déjà pour la date calendaire courante, le run est skippé
               avec `status=already_generated` (pas d'appel Opus, pas d'email).

    Returns:
        Résumé d'exécution (steps, brief_id, timings).
    """
    run_id = _start_run(trigger)
    logger.info(f"Pipeline run #{run_id} démarré (trigger={trigger}, force={force})")
    events.publish(run_id, "run.started", trigger=trigger, force=force)

    # Idempotence : si un brief existe déjà pour aujourd'hui et qu'on n'a pas
    # explicitement demandé à régénérer, on skippe. Le cron quotidien passe
    # toujours par ici — donc un 2e déclenchement cron (ex: redémarrage
    # Railway avec misfire coalesce) n'envoie pas de 2e email.
    if not force:
        settings = get_settings()
        tz = ZoneInfo(settings.timezone)
        today = datetime.now(tz)
        with get_session() as s:
            existing = _find_brief_for_date(s, today)
            if existing is not None:
                logger.info(
                    f"Pipeline run #{run_id} skip — brief #{existing.id} "
                    f"(rev {existing.revision}) existe déjà pour aujourd'hui"
                )
                _end_run(run_id, status="already_generated",
                         summary={"existing_brief_id": existing.id,
                                  "existing_revision": existing.revision},
                         brief_id=existing.id)
                events.publish(run_id, "run.done", status="already_generated",
                               brief_id=existing.id,
                               message=f"Brief rev {existing.revision} déjà généré")
                events.mark_run_done(run_id)
                return {
                    "run_id": run_id, "status": "already_generated",
                    "brief_id": existing.id, "revision": existing.revision,
                }

    with _pipeline_lock() as acquired:
        if not acquired:
            logger.warning(f"Pipeline run #{run_id} skippé — lock déjà détenu")
            _end_run(run_id, status="skipped_locked", summary={"reason": "lock_held"})
            events.publish(run_id, "run.done", status="skipped_locked",
                           error="lock déjà détenu par un autre run")
            events.mark_run_done(run_id)
            return {"run_id": run_id, "status": "skipped", "reason": "lock_held"}

        try:
            summary = _run_pipeline_body(run_id)
            # D-5 : si la synthèse a produit un payload stub, le run n'est PAS
            # "success" — le brief existe en DB mais n'a été livré à personne.
            run_status = "failed_synthesis" if summary.get("synthesis_failed") else "success"
            _end_run(
                run_id,
                status=run_status,
                summary=summary,
                brief_id=summary.get("brief_id"),
            )
            summary["run_id"] = run_id
            summary["status"] = run_status
            events.publish(run_id, "run.done", status=run_status,
                           brief_id=summary.get("brief_id"),
                           revision=summary.get("revision"))
            return summary
        except Exception as exc:
            logger.exception(f"Pipeline run #{run_id} échoué")
            _end_run(
                run_id,
                status="failed",
                error=traceback.format_exc(),
                summary={"error": str(exc)},
            )
            events.publish(run_id, "run.done", status="failed", error=str(exc))
            raise
        finally:
            events.mark_run_done(run_id)


# --- Pipeline body ----------------------------------------------------------

def _run_pipeline_body(run_id: int) -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    date_str = format_date_fr(now_local)

    summary: dict = {"started_at": now_local.isoformat(), "steps": []}
    logger.info(f"=== Pipeline BRVM — {date_str} ===")

    # 1. Collecte
    events.publish(run_id, "step.start", step="collect")
    collection_results = _collect_all(run_id)
    # Détail par source — permet la vue "Par source" dans l'UI admin sans
    # schema change. `news_urls` est la clé naturelle pour JOIN la table `news`
    # ensuite (cf. GET /api/runs/{id}/sources). Borné à 100 URLs par source
    # pour éviter de gonfler PipelineRun.summary (RSS verbeux type lefaso).
    sources_detail = [
        {
            "source_key": r.source_key,
            "news_count": len(r.news),
            "quotes_count": len(r.quotes),
            "errors": r.errors,
            "news_urls": [n.url for n in r.news[:100] if n.url],
        }
        for r in collection_results
    ]
    collect_summary = {
        "step": "collect",
        "sources": len(collection_results),
        "news_count": sum(len(r.news) for r in collection_results),
        "quotes_count": sum(len(r.quotes) for r in collection_results),
        "errors": [e for r in collection_results for e in r.errors],
        "by_source": sources_detail,
    }
    summary["steps"].append(collect_summary)
    # Raccourci de top-level pour que l'endpoint /api/runs/{id}/sources trouve
    # ça rapidement sans avoir à scanner steps[].
    summary["by_source"] = sources_detail
    events.publish(run_id, "step.done", step="collect",
                   sources=collect_summary["sources"],
                   news=collect_summary["news_count"],
                   quotes=collect_summary["quotes_count"])

    # 2. Persistance brute
    events.publish(run_id, "step.start", step="persist")
    new_news_items, new_urls_by_source = _persist_collection(collection_results)
    # Enrichit `sources_detail` avec la liste des news **nouvellement** insérées
    # (discrimine les doublons déjà en DB des vraies nouvelles captures).
    for entry in sources_detail:
        entry["new_news_urls"] = new_urls_by_source.get(entry["source_key"], [])
        entry["new_news_count"] = len(entry["new_news_urls"])
    summary["steps"].append({"step": "persist", "new_news": len(new_news_items)})
    events.publish(run_id, "step.done", step="persist", new_news=len(new_news_items))

    # 3. Enrichissement (Sonnet)
    events.publish(run_id, "step.start", step="enrich", total=len(new_news_items))
    enriched = _enrich_news(new_news_items, run_id=run_id)
    summary["steps"].append({"step": "enrich", "count": len(enriched)})
    events.publish(run_id, "step.done", step="enrich", enriched=len(enriched))

    # 4. Préparation données pour Opus
    events.publish(run_id, "step.start", step="snapshot")
    market_snapshot = _build_market_snapshot()
    historical_context = _build_historical_context()
    # Contexte fondamental par ticker mentionné : permet à Opus de chiffrer
    # price_current / target / valuation sans inventer.
    ticker_fundamentals = _build_ticker_fundamentals(enriched)
    events.publish(run_id, "step.done", step="snapshot",
                   quotes=market_snapshot.get("quotes_count", 0),
                   tickers_with_fundamentals=len(ticker_fundamentals))

    # 5. Synthèse (modèle principal = settings.model_synthesis)
    events.publish(run_id, "step.start", step="synthesize")
    brief_json = BriefSynthesizer().synthesize(
        market_snapshot=market_snapshot,
        enriched_news=enriched,
        historical_context=historical_context,
        ticker_fundamentals=ticker_fundamentals,
    )
    opp_count = len(brief_json.get("opportunities", []))
    synthesis_failed = bool(brief_json.get("_error"))
    logger.info(
        f"Brief synthétisé : {opp_count} opportunités"
        + (" [ÉCHEC SYNTHÈSE — pas de livraison]" if synthesis_failed else "")
    )
    summary["steps"].append({
        "step": "synthesize",
        "opportunities": opp_count,
        "failed": synthesis_failed,
    })
    events.publish(run_id, "step.done", step="synthesize",
                   opportunities=opp_count, failed=synthesis_failed)

    # 5bis. Q-1 A/B : appel modèle alternatif si activé (pour compare blind
    # ultérieur). Best-effort : toute erreur est absorbée pour ne pas
    # bloquer le brief principal.
    alt_payload: dict | None = None
    alt_model: str | None = None
    if settings.ab_test_synthesis and settings.ab_test_model != settings.model_synthesis:
        alt_model = settings.ab_test_model
        try:
            logger.info(f"A/B Q-1 : appel modèle alternatif = {alt_model}")
            alt_payload = BriefSynthesizer(model=alt_model).synthesize(
                market_snapshot=market_snapshot,
                enriched_news=enriched,
                historical_context=historical_context,
                ticker_fundamentals=ticker_fundamentals,
            )
            summary["steps"].append({
                "step": "synthesize_alt",
                "model": alt_model,
                "opportunities": len(alt_payload.get("opportunities", [])),
                "failed": bool(alt_payload.get("_error")),
            })
        except Exception:
            logger.exception(f"A/B Q-1 : échec de l'appel modèle alt {alt_model}")
            alt_payload = None

    # 6. Persistance du brief (upsert par date — revision > 1 sur re-run).
    # Même en cas d'échec synthèse : on persiste pour forensic + consultation
    # admin UI, mais avec delivery_status="failed_synthesis" pour signaler.
    events.publish(run_id, "step.start", step="persist_brief")
    brief_id, revision = _persist_brief(
        brief_json, now_local, synthesis_failed=synthesis_failed,
        payload_alt=alt_payload, model_alt=alt_model,
    )
    summary["brief_id"] = brief_id
    summary["revision"] = revision
    events.publish(run_id, "step.done", step="persist_brief",
                   brief_id=brief_id, revision=revision)

    # 7. Livraison — SKIP si la synthèse a échoué (ADR/D-5) : on NE livre JAMAIS
    # un payload stub aux destinataires. L'admin verra le brief en UI avec
    # delivery_status="failed_synth" et Sentry/logs remontent le
    # détail. Alerte admin email séparée = story D-1.
    if synthesis_failed:
        logger.warning(
            f"Livraison brief #{brief_id} SKIPPÉE — synthèse en échec "
            f"(reason={brief_json.get('skip_reasons', 'unknown')!r})"
        )
        summary["steps"].append({
            "step": "deliver",
            "skipped": True,
            "reason": "synthesis_failed",
        })
        summary["synthesis_failed"] = True
        events.publish(run_id, "step.done", step="deliver",
                       skipped=True, reason="synthesis_failed")
    else:
        events.publish(run_id, "step.start", step="deliver")
        delivery = _deliver(
            brief_json, date_str, brief_id,
            market_snapshot=market_snapshot, revision=revision,
        )
        summary["steps"].append({"step": "deliver", **delivery})
        events.publish(run_id, "step.done", step="deliver",
                       email=delivery.get("email", False),
                       whatsapp=delivery.get("whatsapp", False))

    summary["ended_at"] = datetime.now(tz).isoformat()
    logger.info(f"=== Pipeline terminé — brief_id={brief_id} ===")
    return summary


# --- Étapes détaillées ------------------------------------------------------

def _collect_all(run_id: int | None = None) -> list[CollectionResult]:
    """Lance tous les collectors actifs. Émet des events source.start/done."""
    results: list[CollectionResult] = []
    with get_session() as s:
        sources = s.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
        for src in sources:
            collector = build_collector(src.type, {**src.config, "url": src.url})
            if not collector:
                logger.warning(f"Collector introuvable pour {src.key}")
                continue
            logger.info(f"Collecte → {src.key}")
            if run_id is not None:
                events.publish(run_id, "source.start",
                               source_key=src.key, source_type=src.type)
            collector.source_key = src.key
            # Les collectors qui supportent run_id (sika_quotes, ...) émettent
            # des events fin-granulaire. Les RSS collectors ignorent le kwarg.
            try:
                res = collector.collect(run_id=run_id)
            except TypeError:
                # Collector legacy dont la signature n'accepte pas run_id
                res = collector.collect()
            res.source_key = src.key
            results.append(res)

            src.last_collected_at = datetime.now(UTC)
            src.last_status = "ok" if res.success else "error"
            src.last_error = "; ".join(res.errors[:3]) if res.errors else None

            if run_id is not None:
                events.publish(run_id, "source.done",
                               source_key=src.key,
                               news=len(res.news),
                               quotes=len(res.quotes),
                               errors=res.errors[:3])
    return results


def _persist_collection(
    results: list[CollectionResult],
) -> tuple[list[NewsItem], dict[str, list[str]]]:
    """Persiste cotations + news.

    Retourne :
        - la liste des NewsItem nouvellement insérés (tous sources confondues)
        - un dict `{source_key: [new_url, ...]}` pour discriminer les news
          vraiment nouvelles par source dans PipelineRun.summary.
    """
    new_news: list[NewsItem] = []
    new_urls_by_source: dict[str, list[str]] = {}
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
                    # Upsert complet — on patche chaque champ explicitement
                    # (is not None) pour ne pas écraser avec une valeur nulle
                    # ni perdre une variation "réelle" à 0%.
                    if q.close_price is not None:
                        existing.close_price = q.close_price
                    if q.variation_pct is not None:
                        existing.variation_pct = q.variation_pct
                    if q.volume is not None:
                        existing.volume = q.volume
                    if q.value_traded is not None:
                        existing.value_traded = q.value_traded
                    if q.extras:
                        existing.extras = q.extras
                    if q.country:
                        existing.country = q.country
                    if q.name:
                        existing.name = q.name
                    if q.sector:
                        existing.sector = q.sector
                else:
                    s.add(Quote(
                        ticker=q.ticker, name=q.name, sector=q.sector,
                        country=q.country,
                        quote_date=q.quote_date, close_price=q.close_price,
                        variation_pct=q.variation_pct, volume=q.volume,
                        value_traded=q.value_traded,
                        extras=q.extras,
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
                new_urls_by_source.setdefault(result.source_key, []).append(n.url)
    return new_news, new_urls_by_source


def _enrich_news(articles: list[NewsItem], run_id: int | None = None) -> list[dict]:
    """Enrichit les nouveaux articles via Sonnet + recharge le contexte récent.

    Émet un event `article.done` / `article.error` par item pour la vue live.
    """
    settings = get_settings()
    enriched_list: list[dict] = []

    if articles:
        enricher = NewsEnricher()
        total = len(articles)
        with get_session() as s:
            for i, article in enumerate(articles, start=1):
                if run_id is not None:
                    events.publish(run_id, "article.start",
                                   index=i, total=total, title=article.title[:120])
                data = enricher.enrich(article)
                if "error" in data:
                    if run_id is not None:
                        events.publish(run_id, "article.error",
                                       index=i, title=article.title[:120],
                                       error=str(data.get("error"))[:200])
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
                if run_id is not None:
                    events.publish(run_id, "article.done",
                                   index=i, title=article.title[:120],
                                   tickers=data.get("tickers_mentioned", []),
                                   materiality=data.get("materiality"))

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
    """Snapshot marché pour le prompt Opus.

    Délègue à `analysis.market.build_snapshot` (source of truth unique) puis
    repackage en champs compacts attendus par le prompt. Évite d'avoir 2
    logiques divergentes — l'ancienne version incluait les titres non-tradés
    dans `top_gainers` (→ faux 0% movers envoyés à Opus).
    """
    from .analysis.market import build_snapshot  # import local : évite cycles
    with get_session() as s:
        snap = build_snapshot(s)
        if snap.get("quotes_count", 0) == 0:
            return {"note": "Aucune cotation en DB"}

        def _compact(row: dict) -> dict:
            return {
                "ticker": row["ticker"],
                "name": row["name"],
                "close": row["close_price"],
                "var_pct": row["variation_pct"],
                "volume": row["volume"],
            }

        # Rotation sectorielle 5 jours — utile pour que Opus argumente
        # "banques outperform télécoms cette semaine" plutôt que de deviner.
        sector_rotation = compute_sector_rotation(s, lookback_days=5)

        return {
            "date": snap["trading_date"],
            "quotes_count": snap["quotes_count"],
            "traded_count": snap["traded_count"],
            "top_gainers": [_compact(r) for r in snap["movers_up"]],
            "top_losers":  [_compact(r) for r in snap["movers_down"]],
            "top_volumes": [_compact(r) for r in snap["top_volumes"]],
            "sector_rotation_5d": sector_rotation,
        }


def _build_ticker_fundamentals(enriched_news: list[dict]) -> list[dict]:
    """Fondamentaux + features techniques par ticker mentionné dans les news.

    Pour chaque ticker extrait des `enriched_news`, on assemble :
      - Fondamentaux (close, PER, dividende, market cap, beta, RSI — extras Sika)
      - Features techniques calculées depuis l'historique des quotes :
        MA20/50 + trend, Bollinger position, ATR%, volume ratio, 52w hi/lo,
        momentum 1w/1m. Voir `analysis/features.py`.

    Si aucun ticker dans les news → liste vide, Opus se rabat sur le
    market_snapshot. Cap à 30 tickers (au-delà, le payload grossit sans valeur
    ajoutée — Opus ne recommandera pas 30 titres dans un brief).
    """
    candidates: set[str] = set()
    for item in enriched_news:
        for t in (item.get("tickers_mentioned") or []):
            if isinstance(t, str) and t.strip():
                candidates.add(t.strip().upper())
    if not candidates:
        return []

    out: list[dict] = []
    with get_session() as s:
        for ticker in sorted(candidates)[:30]:
            q = s.execute(
                select(Quote)
                .where(Quote.ticker == ticker)
                .order_by(Quote.quote_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if not q:
                continue
            # Features techniques — dict potentiellement partiel selon l'historique
            tech = compute_technical_features(ticker, s)
            entry = {
                "ticker": q.ticker,
                "name": q.name or "",
                "sector": q.sector or "",
                "country": q.country or "",
                "close_price": q.close_price,
                "previous_close": (q.extras or {}).get("previous_close"),
                "variation_pct": q.variation_pct,
                "volume_shares": q.volume,
                "per": (q.extras or {}).get("per"),
                "dividend": (q.extras or {}).get("dividend"),
                "dividend_yield_pct": (q.extras or {}).get("dividend_yield_pct"),
                "market_cap_mfcfa": (q.extras or {}).get("market_cap_mfcfa"),
                "beta_1y": (q.extras or {}).get("beta_1y"),
                "rsi": (q.extras or {}).get("rsi"),
            }
            # Les features techniques sont mergées directement — Opus les voit
            # comme des champs first-class (ma_trend, bollinger_position, …)
            entry.update(tech)
            out.append(entry)
    return out


def _build_historical_context(days: int = 5) -> list[dict]:
    """N derniers briefs + leurs tickers signalés. Eager-load pour éviter N+1."""
    from sqlalchemy.orm import selectinload
    with get_session() as s:
        briefs = s.execute(
            select(Brief)
            .options(selectinload(Brief.signals))
            .order_by(Brief.brief_date.desc())
            .limit(days)
        ).scalars().all()
        return [{
            "date": b.brief_date.isoformat() if b.brief_date else None,
            "summary": b.payload.get("market_summary", "") if b.payload else "",
            "tickers": [sig.ticker for sig in b.signals],
        } for b in briefs]


def _find_brief_for_date(
    session,
    local_date,
    *,
    brief_type: str = "daily",
) -> Brief | None:
    """Retourne le brief existant pour `(date calendaire, brief_type)`, ou None.

    On matche sur la plage journalière [local_date, local_date+1) plutôt que
    sur l'égalité stricte (les timestamps stockés ont une heure). Le filtrage
    par `brief_type` est crucial pour que lundi puisse porter à la fois le
    brief daily et — potentiellement plus tard si on déplace l'horaire — un
    brief weekly, sans collision.
    """
    from datetime import time
    # Normalise en début/fin de journée calendaire. local_date peut être un
    # datetime ou un date.
    if hasattr(local_date, "date"):
        day_start = local_date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        day_start = datetime.combine(local_date, time.min).replace(tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    return session.execute(
        select(Brief)
        .where(Brief.brief_date >= day_start)
        .where(Brief.brief_date < day_end)
        .where(Brief.brief_type == brief_type)
    ).scalar_one_or_none()


def _persist_brief(
    brief_json: dict,
    now_local: datetime,
    *,
    synthesis_failed: bool = False,
    payload_alt: dict | None = None,
    model_alt: str | None = None,
) -> tuple[int, int]:
    """Upsert du brief par date calendaire + gel des signals à la revision 1.

    Retour : `(brief_id, revision)`. `revision == 1` = nouveau brief du jour,
    `revision > 1` = mise à jour textuelle (les signals ne sont PAS touchés).

    Si `synthesis_failed=True` (payload stub) : le brief est quand même
    persisté pour forensic, mais `delivery_status="failed_synth"`
    bloque toute livraison (voir pipeline D-5). Les signals ne sont PAS
    créés à partir d'un payload stub.

    Rationale : séparer "analyse textuelle" (mutable via re-run) de "signaux
    actionnables" (immutables pour backtest honnête). Un investisseur qui lit
    le brief rev 2 voit la dernière lecture d'Opus, mais les recos gelées
    restent les premières émises — conforme à la pratique des desks de
    recherche sell-side.
    """
    with get_session() as s:
        existing = _find_brief_for_date(s, now_local)

        if existing:
            # Révision — on met à jour le texte et la livraison est remise à
            # "pending" pour que `_deliver` renvoie un email mentionnant
            # explicitement "Révision N".
            existing.revision += 1
            existing.revised_at = datetime.now(UTC)
            existing.summary_markdown = brief_json.get("market_summary", "")
            existing.payload = brief_json
            if synthesis_failed:
                existing.delivery_status = "failed_synth"
                existing.email_sent = False
                existing.whatsapp_sent = False
                existing.delivery_errors = (
                    brief_json.get("skip_reasons") or "synthesis stub"
                )[:2000]
            else:
                existing.delivery_status = "pending"
                existing.email_sent = False
                existing.whatsapp_sent = False
                existing.delivery_errors = None
            # Q-1 : toujours overwrite le payload_alt sur révision (suit le texte)
            if payload_alt is not None:
                existing.payload_alt = payload_alt
                existing.model_alt = model_alt
            s.flush()
            logger.info(f"Brief #{existing.id} mis à jour en révision {existing.revision}")
            return existing.id, existing.revision

        # Nouveau brief du jour (revision=1) + signals + price_at_signal
        brief = Brief(
            brief_date=now_local,
            summary_markdown=brief_json.get("market_summary", ""),
            payload=brief_json,
            revision=1,
            delivery_status=(
                "failed_synth" if synthesis_failed else "pending"
            ),
            delivery_errors=(
                (brief_json.get("skip_reasons") or "synthesis stub")[:2000]
                if synthesis_failed else None
            ),
            payload_alt=payload_alt,
            model_alt=model_alt,
        )
        s.add(brief)
        s.flush()

        # Pas de signals créés sur un payload stub : les "opportunities" d'un
        # _error_payload sont vides par design, mais on court-circuite par
        # sécurité si le prompt venait à changer.
        if synthesis_failed:
            return brief.id, 1

        for opp in brief_json.get("opportunities", []):
            ticker = opp.get("ticker")
            if not ticker:
                continue
            last_quote = s.execute(
                select(Quote).where(Quote.ticker == ticker)
                .order_by(Quote.quote_date.desc()).limit(1)
            ).scalar_one_or_none()

            price_ref: float | None = None
            if last_quote:
                if last_quote.close_price and last_quote.close_price > 0:
                    price_ref = last_quote.close_price
                else:
                    prev = (last_quote.extras or {}).get("previous_close")
                    if prev and prev > 0:
                        price_ref = float(prev)

            s.add(Signal(
                brief_id=brief.id,
                ticker=ticker,
                direction=opp.get("direction", "watch"),
                conviction=int(opp.get("conviction", 3)),
                thesis=opp.get("thesis", ""),
                price_at_signal=price_ref,
            ))
        s.flush()
        return brief.id, 1


def _deliver(
    brief_json: dict,
    date_str: str,
    brief_id: int,
    *,
    market_snapshot: dict | None = None,
    revision: int = 1,
    email_recipients_override: list[tuple[str, str | None]] | None = None,
    brief_type: str = "daily",
) -> dict:
    """Envoie email + WhatsApp. Met à jour Brief.delivery_status en conséquence.

    Si `revision > 1`, le sujet email mentionne "Révision N" et une bannière
    est affichée en tête pour que le lecteur sache que c'est une mise à jour.

    Si `email_recipients_override` est fourni, l'envoi cible cette liste
    d'adresses (mode "ciblage ad-hoc" depuis l'admin). Dans ce mode on skip
    WhatsApp (pas de ciblage ad-hoc sur ce canal) et on ne touche PAS au
    `delivery_status` du brief — c'est un envoi ponctuel, pas le statut
    officiel du brief.

    Retourne un dict récap pour le summary du run.
    """
    email_ok = False
    whatsapp_attempted = False
    whatsapp_ok = False
    errors: list[str] = []
    sent_addresses: list[str] = []
    is_targeted = email_recipients_override is not None

    # Détermine les fréquences cibles (daily+critical selon conviction, ou all pour weekly).
    # Bypass en mode ciblé — un envoi ad-hoc ignore les préférences destinataire.
    from .delivery.repository import frequencies_for_brief
    target_frequencies = (
        None if is_targeted
        else frequencies_for_brief(brief_type, brief_json)
    )

    # Email (toujours tenté — obligatoire)
    try:
        subject, html = render_email_html(
            brief_json,
            date_str,
            market_snapshot=market_snapshot,
            edition_num=brief_id,
            revision=revision,
            brief_type=brief_type,
        )
        sent_addresses = EmailSender().send(
            subject, html,
            recipients_override=email_recipients_override,
            frequencies=target_frequencies,
        )
        email_ok = True
    except Exception as e:
        errors.append(f"email: {e}")
        logger.exception("Échec envoi email")

    # WhatsApp (optionnel, et skipé si on cible des destinataires spécifiques)
    if not is_targeted:
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

    # En mode ciblé : ne pas écraser le statut officiel du brief (envoi ad-hoc).
    if not is_targeted:
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

    return {
        "status": status,
        "email_ok": email_ok,
        "whatsapp_ok": whatsapp_ok,
        "errors": errors,
        "sent_to": sent_addresses,
    }


# --- Redelivery (retry manuel depuis l'UI admin) ----------------------------

class RedeliveryError(RuntimeError):
    """Levée quand un brief ne peut pas être rejoué (stub, inexistant, etc.)."""


def redeliver_brief(
    brief_id: int,
    *,
    email_recipients_override: list[tuple[str, str | None]] | None = None,
) -> dict:
    """Rejoue la livraison email + WhatsApp pour un brief déjà persisté.

    Utilisé par `POST /api/briefs/{brief_id}/redeliver` quand l'envoi initial
    a échoué (ex : timeout SMTP Brevo). Ne relance PAS la synthèse Opus ni
    n'incrémente la révision — le brief reste identique, seule la livraison
    est retentée.

    Si `email_recipients_override` est fourni, on envoie uniquement à cette
    liste `(address, name)` et on ne touche pas au statut officiel du brief
    (mode "ciblage ad-hoc" — ex : renvoyer à un nouveau destinataire ou
    ré-essayer sur un seul qui avait échoué). WhatsApp est skipé dans ce mode.

    Contrairement au run cron, on ne crée pas de nouveau `PipelineRun` : la
    table `briefs` porte déjà `delivery_status` / `delivery_errors` et les
    champs `email_sent` / `whatsapp_sent` qui sont tous mis à jour par
    `_deliver`. Pour un audit-trail plus riche plus tard, envisager une table
    `delivery_attempts` (cf. backlog).

    Raises:
        RedeliveryError: si le brief n'existe pas, ou si `delivery_status`
            vaut `failed_synth` (payload stub — on ne livre jamais ça).
    """
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    # Lecture en session courte — on ne garde pas la session ouverte pendant
    # le SMTP (qui peut prendre jusqu'à 30s par destinataire).
    with get_session() as s:
        brief = s.get(Brief, brief_id)
        if brief is None:
            raise RedeliveryError(f"Brief #{brief_id} introuvable")
        if brief.delivery_status == "failed_synth":
            raise RedeliveryError(
                f"Brief #{brief_id} = payload stub (synthèse échouée) — "
                "on ne livre jamais ça. Relance le pipeline entier."
            )
        brief_json = brief.payload
        brief_date_local = brief.brief_date.astimezone(tz)
        revision = brief.revision
        brief_type = brief.brief_type

    date_str = format_date_fr(brief_date_local)

    # Snapshot marché : pertinent uniquement pour les briefs daily (section
    # "Top gainers/losers"). Le weekly a son propre contexte rétrospectif.
    market_snapshot = _build_market_snapshot() if brief_type == "daily" else None

    mode = "ciblé" if email_recipients_override else "standard"
    logger.info(
        f"Rejouer livraison brief #{brief_id} ({brief_type}, "
        f"revision {revision}, mode {mode})"
    )
    return _deliver(
        brief_json, date_str, brief_id,
        market_snapshot=market_snapshot, revision=revision,
        email_recipients_override=email_recipients_override,
        brief_type=brief_type,
    )


# ============================================================================
# Pipeline HEBDOMADAIRE — audit 7j + scorecard P&L
# ============================================================================

WEEKLY_LOCK_KEY = "brvm_weekly_pipeline"


def _most_recent_friday(ref: datetime) -> datetime:
    """Retourne le dernier vendredi <= ref (même jour si ref est un vendredi).

    Python weekday : lundi=0, ..., vendredi=4, samedi=5, dimanche=6.
    """
    # Distance jusqu'au dernier vendredi : (weekday - 4) mod 7
    days_since_friday = (ref.weekday() - 4) % 7
    return (ref - timedelta(days=days_since_friday)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _build_plays_with_pnl(
    daily_briefs: list[Brief],
    latest_close_by_ticker: dict[str, float],
) -> list[dict]:
    """Pour chaque signal des briefs daily de la fenêtre, construit une ligne
    `play` enrichie du P&L réel (signe corrigé pour `avoid`).

    Formule :
      - `direction = buy`  : pnl_pct = (current - signal) / signal × 100
      - `direction = avoid`: pnl_pct = (signal - current) / signal × 100  (inversé)
      - autres (watch/hold) : pnl_pct = (current - signal) / signal × 100
        (neutre directionnellement, mais on garde la valeur pour contexte)

    Un signal sans `price_at_signal` (prix de référence manquant au moment du
    brief) est exclu — on ne peut pas calculer de P&L fiable.
    """
    plays: list[dict] = []
    for brief in daily_briefs:
        # On préfère reconstruire depuis `payload.opportunities` plutôt que
        # `brief.signals` — le payload porte sector/name/thesis complets
        # qu'on voudra citer dans le weekly, là où Signal n'a que thesis.
        issued_on = brief.brief_date.date().isoformat()
        opps = (brief.payload or {}).get("opportunities") or []
        # On a aussi besoin du prix au signal (stocké dans Signal, pas dans
        # payload) — on construit un lookup par ticker.
        price_by_ticker = {
            s.ticker: s.price_at_signal for s in brief.signals
            if s.price_at_signal
        }
        for opp in opps:
            if not isinstance(opp, dict):
                continue
            ticker = (opp.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            direction = opp.get("direction", "watch")
            # Les directions non-actionnables (watch/hold) sont incluses pour
            # contexte narratif mais ne rentrent PAS dans le scorecard (le LLM
            # et le reconcile les excluent via le filtre direction).
            signal_price = price_by_ticker.get(ticker)
            if not signal_price or signal_price <= 0:
                continue
            current_price = latest_close_by_ticker.get(ticker)
            if current_price is None or current_price <= 0:
                # Pas de quote plus récente : skip (on ne peut rien dire).
                continue
            raw_pnl = (current_price - signal_price) / signal_price * 100
            if direction == "avoid":
                raw_pnl = -raw_pnl  # inverse : baisse du titre = gain pour l'avoid
            days_held = (datetime.now(UTC).date() - brief.brief_date.date()).days
            plays.append({
                "ticker": ticker,
                "name": opp.get("name", ""),
                "sector": opp.get("sector", ""),
                "direction": direction,
                "conviction": int(opp.get("conviction", 3)),
                "issued_on": issued_on,
                "thesis": opp.get("thesis", ""),
                "price_at_signal": round(signal_price, 2),
                "current_price": round(current_price, 2),
                "realized_pnl_pct": round(raw_pnl, 2),
                "days_held": max(0, days_held),
            })
    return plays


def _build_user_trades_context(
    week_start: datetime,
    week_end: datetime,
    latest_close_by_ticker: dict[str, float],
) -> dict:
    """Charge les trades utilisateur (auto-reportés) de la semaine et calcule
    l'attribution signal vs intuition + P&L non-réalisé mark-to-market.

    Structure de retour :
    ```
    {
      "trades": [
        {
          "ticker": "BOAC", "action": "buy", "quantity": 50,
          "unit_price": 6550, "executed_at": "2026-04-15",
          "reason": "brief",  # brief | intuition | news | other
          "linked_brief_id": 42, "linked_signal_id": 118,
          "current_close": 6780,
          "unrealized_pnl_pct": 3.51,      # pour les buys, mark-to-market
          "notes": "…"
        }
      ],
      "stats": {
        "total": 3, "following_signal": 2, "autonomous": 1,
        "avg_unrealized_pnl_pct": 1.87,
      }
    }
    ```

    Note : on ne tente PAS de matcher buy↔sell (FIFO) pour un P&L fermé —
    c'est un exercice complexe qui mérite son propre service de portefeuille.
    Ici on se contente du mark-to-market sur les buys ouverts — suffisant
    pour qu'Opus observe "le call BOAC suivi par un achat s'est bien passé".
    """
    upper = week_end + timedelta(days=1)

    # Extraction immédiate en dict pour éviter DetachedInstanceError après
    # fermeture de la session SQLAlchemy.
    raw_trades: list[dict] = []
    with get_session() as s:
        for t in s.execute(
            select(Trade)
            .where(Trade.executed_at >= week_start)
            .where(Trade.executed_at < upper)
            .order_by(Trade.executed_at)
        ).scalars().all():
            raw_trades.append({
                "ticker": (t.ticker or "").upper(),
                "action": t.action,
                "quantity": t.quantity,
                "unit_price": t.unit_price,
                "executed_at": t.executed_at.date().isoformat(),
                "reason": t.reason,
                "brief_id": t.brief_id,
                "signal_id": t.signal_id,
                "notes": (t.notes or "")[:200],
            })

    out_trades: list[dict] = []
    pnls: list[float] = []
    for t in raw_trades:
        ticker = t["ticker"]
        current = latest_close_by_ticker.get(ticker)
        unrealized_pnl: float | None = None
        if (t["action"] == "buy" and current is not None
                and t["unit_price"] and t["unit_price"] > 0):
            unrealized_pnl = round(
                (current - t["unit_price"]) / t["unit_price"] * 100, 2
            )
            pnls.append(unrealized_pnl)

        out_trades.append({
            "ticker": ticker,
            "name": "",  # non stocké dans Trade — on pourrait le rejoindre plus tard
            "action": t["action"],
            "quantity": t["quantity"],
            "unit_price": t["unit_price"],
            "executed_at": t["executed_at"],
            "reason": t["reason"],
            "linked_brief_id": t["brief_id"],
            "linked_signal_id": t["signal_id"],
            "current_close": current,
            "unrealized_pnl_pct": unrealized_pnl,
            "notes": t["notes"],
        })

    following_signal = sum(
        1 for t in raw_trades
        if t["reason"] == "brief" or t["signal_id"] is not None
    )
    avg_pnl = round(sum(pnls) / len(pnls), 2) if pnls else None

    return {
        "trades": out_trades,
        "stats": {
            "total": len(raw_trades),
            "following_signal": following_signal,
            "autonomous": len(raw_trades) - following_signal,
            "avg_unrealized_pnl_pct": avg_pnl,
        },
    }


def _build_weekly_context(
    week_start: datetime,
    week_end: datetime,
) -> dict:
    """Charge tout ce dont le WeeklyBriefSynthesizer a besoin pour la fenêtre
    `[week_start, week_end]` (inclusif sur week_end).

    Retourne un dict prêt à passer à `WeeklyBriefSynthesizer.synthesize()`.
    """
    # Borne supérieure exclusive pour éviter la zone grise de fin de journée.
    upper = week_end + timedelta(days=1)

    with get_session() as s:
        # 1. Briefs daily de la semaine
        daily_briefs = list(s.execute(
            select(Brief)
            .where(Brief.brief_type == "daily")
            .where(Brief.brief_date >= week_start)
            .where(Brief.brief_date < upper)
            .order_by(Brief.brief_date)
        ).scalars().all())

        # 2. Dernier close par ticker (pour calculer le P&L réel)
        # On veut le close le plus récent, pas nécessairement dans la fenêtre
        # (le vendredi de clôture peut être après le dernier brief de la semaine).
        quotes = s.execute(
            select(Quote.ticker, Quote.close_price, Quote.quote_date)
            .order_by(Quote.ticker, Quote.quote_date.desc())
        ).all()
        latest_close: dict[str, float] = {}
        for ticker, close_price, _qd in quotes:
            if ticker not in latest_close and close_price and close_price > 0:
                latest_close[ticker] = float(close_price)

        # 3. Mouvement de la semaine par ticker (open/close/volume)
        week_quotes_rows = s.execute(
            select(Quote.ticker, Quote.close_price, Quote.quote_date, Quote.volume)
            .where(Quote.quote_date >= week_start)
            .where(Quote.quote_date < upper)
            .order_by(Quote.ticker, Quote.quote_date)
        ).all()
        by_ticker: dict[str, list] = {}
        for ticker, close_price, qd, volume in week_quotes_rows:
            by_ticker.setdefault(ticker, []).append({
                "close": close_price, "date": qd.date().isoformat(),
                "volume": volume or 0,
            })
        week_quotes = []
        for ticker, rows in by_ticker.items():
            if not rows:
                continue
            first, last = rows[0], rows[-1]
            if not first["close"] or first["close"] <= 0:
                continue
            change_pct = round(
                (last["close"] - first["close"]) / first["close"] * 100, 2,
            )
            week_quotes.append({
                "ticker": ticker,
                "open_week": round(first["close"], 2),
                "close_week": round(last["close"], 2),
                "change_pct": change_pct,
                "volume_total": sum(r["volume"] for r in rows),
            })
        week_quotes.sort(key=lambda q: abs(q["change_pct"]), reverse=True)

        # 4. News enrichies "structurelles" : on prend les articles enrichis de
        # la semaine avec un score d'importance >= 3 (si présent dans l'enrichment)
        # ou les tickers mentionnés >= 1 (heuristique large pour le 1er jet).
        news_rows = list(s.execute(
            select(NewsArticle)
            .where(NewsArticle.enriched_at.is_not(None))
            .where(NewsArticle.enriched_at >= week_start)
            .where(NewsArticle.enriched_at < upper)
            .order_by(NewsArticle.enriched_at.desc())
            .limit(30)
        ).scalars().all())
        week_news = []
        for art in news_rows:
            enr = art.enrichment or {}
            importance = enr.get("importance") or enr.get("relevance_score")
            # Filtre "structurel" : importance élevée OU mention de catalyseur
            is_structural = (
                (isinstance(importance, (int, float)) and importance >= 3)
                or bool(enr.get("catalysts"))
                or bool(enr.get("regulation"))
            )
            if not is_structural:
                continue
            week_news.append({
                "title": art.title,
                "published_at": art.published_at.date().isoformat() if art.published_at else None,
                "tickers": art.tickers_mentioned or [],
                "summary": enr.get("summary", "")[:300],
            })

    # 5. plays avec P&L — en dehors de la session, pas d'I/O ici
    plays_with_pnl = _build_plays_with_pnl(daily_briefs, latest_close)

    # Trades utilisateur auto-reportés — attribution signal vs intuition
    user_trades_ctx = _build_user_trades_context(week_start, week_end, latest_close)

    # Représentation minimale des briefs daily pour que Opus ait le contexte
    # narratif sans se noyer dans le détail.
    daily_briefs_repr = [
        {
            "brief_id": b.id,
            "brief_date": b.brief_date.date().isoformat(),
            "market_regime": (b.payload or {}).get("market_regime"),
            "market_summary": (b.payload or {}).get("market_summary", "")[:500],
            "opportunities_count": len((b.payload or {}).get("opportunities") or []),
        }
        for b in daily_briefs
    ]

    return {
        "daily_briefs": daily_briefs_repr,
        "plays_with_pnl": plays_with_pnl,
        "week_quotes": week_quotes[:20],  # cap anti-prompt bloat
        "week_news": week_news,
        "user_trades": user_trades_ctx,
    }


def run_weekly_pipeline(trigger: str = "cron", force: bool = False) -> dict:
    """Orchestrateur du brief hebdomadaire.

    Fenêtre par défaut : la semaine de trading qui vient de se terminer
    (lundi → vendredi le plus récent). Idempotent par `(week_end_date, weekly)` —
    un 2e appel le même samedi ne regénère pas.

    `force=True` : force une nouvelle révision même si un weekly existe déjà
    pour cette fenêtre (typiquement quand l'admin veut tester depuis l'UI).
    """
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    # Lock séparé du daily : les deux pipelines peuvent tourner en parallèle
    # (weekly samedi 7h, daily chaque matin 8h — aucun conflit en pratique).
    conn = engine.connect()
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": WEEKLY_LOCK_KEY},
            ).scalar()
        )
        if not acquired:
            logger.warning("Pipeline weekly skip — advisory lock déjà pris")
            return {"status": "skipped_locked", "brief_id": None}

        run_id = _start_run(trigger, pipeline_type="weekly")
        try:
            return _run_weekly_pipeline_body(run_id, tz, force=force)
        except Exception as e:
            logger.exception("Pipeline weekly en échec")
            _end_run(run_id, status="failed", error=f"{type(e).__name__}: {e}",
                     trace=traceback.format_exc())
            events.publish(run_id, "run.done", status="failed", error=str(e))
            events.mark_run_done(run_id)
            raise
    finally:
        try:
            conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": WEEKLY_LOCK_KEY},
            )
        except Exception:
            pass
        conn.close()


def _run_weekly_pipeline_body(run_id: int, tz: ZoneInfo, *, force: bool) -> dict:
    """Corps du pipeline weekly (lock déjà acquis)."""
    now_local = datetime.now(tz)
    week_end = _most_recent_friday(now_local)
    week_start = (week_end - timedelta(days=4)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    # Horodatage du brief : vendredi 23h59 local — "as-of" de l'audit
    brief_dt = week_end.replace(hour=23, minute=59, second=0)

    logger.info(
        f"Pipeline weekly #{run_id} — fenêtre {week_start.date()} → {week_end.date()}"
    )
    events.publish(run_id, "weekly.start",
                   week_start=week_start.date().isoformat(),
                   week_end=week_end.date().isoformat())

    # Idempotence : si un weekly existe déjà pour ce vendredi ET qu'on ne force pas
    with get_session() as s:
        existing = _find_brief_for_date(s, week_end, brief_type="weekly")
        if existing is not None and not force:
            logger.info(
                f"Pipeline weekly #{run_id} skip — brief hebdo #{existing.id} "
                f"(rev {existing.revision}) déjà présent pour {week_end.date()}"
            )
            _end_run(run_id, status="already_generated",
                     summary={"existing_brief_id": existing.id,
                              "existing_revision": existing.revision,
                              "week_start": week_start.date().isoformat(),
                              "week_end": week_end.date().isoformat()},
                     brief_id=existing.id)
            events.publish(run_id, "run.done", status="already_generated",
                           brief_id=existing.id)
            events.mark_run_done(run_id)
            return {
                "run_id": run_id, "status": "already_generated",
                "brief_id": existing.id, "revision": existing.revision,
            }

    # 1. Charge le contexte (briefs daily de la semaine + P&L réel + news + quotes)
    ctx = _build_weekly_context(week_start, week_end)
    events.publish(run_id, "weekly.context_built",
                   daily_briefs=len(ctx["daily_briefs"]),
                   plays=len(ctx["plays_with_pnl"]),
                   week_quotes=len(ctx["week_quotes"]))

    # Cas dégénéré : aucun brief daily dans la fenêtre → on ne peut pas produire
    # un audit cohérent. On ne crée PAS de brief weekly stub (ça tromperait l'audit).
    if not ctx["daily_briefs"]:
        logger.warning(
            f"Pipeline weekly #{run_id} : aucun brief daily dans la fenêtre, abort"
        )
        _end_run(run_id, status="no_data",
                 summary={"week_start": week_start.date().isoformat(),
                          "week_end": week_end.date().isoformat()})
        events.publish(run_id, "run.done", status="no_data",
                       message="Aucun brief daily dans la fenêtre")
        events.mark_run_done(run_id)
        return {"run_id": run_id, "status": "no_data", "brief_id": None}

    # 2. Synthèse Opus
    synth = WeeklyBriefSynthesizer()
    brief_json = synth.synthesize(
        week_start=week_start.date().isoformat(),
        week_end=week_end.date().isoformat(),
        daily_briefs=ctx["daily_briefs"],
        plays_with_pnl=ctx["plays_with_pnl"],
        week_quotes=ctx["week_quotes"],
        week_news=ctx["week_news"],
        user_trades=ctx["user_trades"],
    )
    synthesis_failed = bool(brief_json.get("_error"))
    events.publish(run_id, "weekly.synthesized",
                   plays=len(brief_json.get("plays") or []),
                   error=synthesis_failed)

    # 3. Persistance (brief_type='weekly', pas de signals — c'est un audit)
    brief_id, revision = _persist_weekly_brief(
        brief_json, brief_dt, synthesis_failed=synthesis_failed,
    )

    # 4. Livraison — template dédié brief_weekly_email.html.j2 (routé via brief_type).
    date_str = format_date_fr(brief_dt)
    delivery = {"status": "skipped", "email_ok": False, "whatsapp_ok": False, "errors": []}
    if not synthesis_failed:
        delivery = _deliver(
            brief_json, date_str, brief_id,
            market_snapshot=None,  # le weekly a son propre contexte, pas de top gainers/losers
            revision=revision,
            brief_type="weekly",
        )
    events.publish(run_id, "weekly.delivered",
                   status=delivery["status"], email_ok=delivery["email_ok"])

    _end_run(run_id, status="success",
             summary={
                 "brief_type": "weekly",
                 "week_start": week_start.date().isoformat(),
                 "week_end": week_end.date().isoformat(),
                 "brief_id": brief_id,
                 "revision": revision,
                 "plays": len(brief_json.get("plays") or []),
                 "delivery_status": delivery["status"],
             }, brief_id=brief_id)
    events.publish(run_id, "run.done", status="success", brief_id=brief_id)
    events.mark_run_done(run_id)
    return {
        "run_id": run_id, "status": "success",
        "brief_id": brief_id, "revision": revision,
        "week_start": week_start.date().isoformat(),
        "week_end": week_end.date().isoformat(),
    }


def _persist_weekly_brief(
    brief_json: dict, brief_dt: datetime, *, synthesis_failed: bool,
) -> tuple[int, int]:
    """Persiste un brief hebdo (brief_type='weekly').

    Contrairement au daily, pas de signals créés (un brief hebdo est
    rétrospectif — les signaux d'origine sont déjà dans les briefs daily).
    Idempotence par `(brief_dt.date(), 'weekly')` via `_find_brief_for_date`.
    """
    with get_session() as s:
        existing = _find_brief_for_date(s, brief_dt, brief_type="weekly")
        if existing:
            existing.revision += 1
            existing.revised_at = datetime.now(UTC)
            existing.summary_markdown = brief_json.get("week_summary", "")
            existing.payload = brief_json
            existing.delivery_status = (
                "failed_synth" if synthesis_failed else "pending"
            )
            existing.email_sent = False
            existing.whatsapp_sent = False
            existing.delivery_errors = (
                (brief_json.get("_raw_preview") or "synthesis stub")[:2000]
                if synthesis_failed else None
            )
            s.flush()
            logger.info(
                f"Brief hebdo #{existing.id} mis à jour en révision {existing.revision}"
            )
            return existing.id, existing.revision

        brief = Brief(
            brief_date=brief_dt,
            brief_type="weekly",
            summary_markdown=brief_json.get("week_summary", ""),
            payload=brief_json,
            revision=1,
            delivery_status=(
                "failed_synth" if synthesis_failed else "pending"
            ),
            delivery_errors=(
                (brief_json.get("_raw_preview") or "synthesis stub")[:2000]
                if synthesis_failed else None
            ),
        )
        s.add(brief)
        s.flush()
        return brief.id, 1
