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
    collect_summary = {
        "step": "collect",
        "sources": len(collection_results),
        "news_count": sum(len(r.news) for r in collection_results),
        "quotes_count": sum(len(r.quotes) for r in collection_results),
        "errors": [e for r in collection_results for e in r.errors],
    }
    summary["steps"].append(collect_summary)
    events.publish(run_id, "step.done", step="collect",
                   sources=collect_summary["sources"],
                   news=collect_summary["news_count"],
                   quotes=collect_summary["quotes_count"])

    # 2. Persistance brute
    events.publish(run_id, "step.start", step="persist")
    new_news_items = _persist_collection(collection_results)
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
    events.publish(run_id, "step.done", step="snapshot",
                   quotes=market_snapshot.get("quotes_count", 0))

    # 5. Synthèse (Opus)
    events.publish(run_id, "step.start", step="synthesize")
    brief_json = BriefSynthesizer().synthesize(
        market_snapshot=market_snapshot,
        enriched_news=enriched,
        historical_context=historical_context,
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

    # 6. Persistance du brief (upsert par date — revision > 1 sur re-run).
    # Même en cas d'échec synthèse : on persiste pour forensic + consultation
    # admin UI, mais avec delivery_status="failed_synthesis" pour signaler.
    events.publish(run_id, "step.start", step="persist_brief")
    brief_id, revision = _persist_brief(
        brief_json, now_local, synthesis_failed=synthesis_failed,
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
    return new_news


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

        return {
            "date": snap["trading_date"],
            "quotes_count": snap["quotes_count"],
            "traded_count": snap["traded_count"],
            "top_gainers": [_compact(r) for r in snap["movers_up"]],
            "top_losers":  [_compact(r) for r in snap["movers_down"]],
            "top_volumes": [_compact(r) for r in snap["top_volumes"]],
        }


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


def _find_brief_for_date(session, local_date) -> Brief | None:
    """Retourne le brief existant pour la date calendaire donnée, ou None.

    On matche sur la plage journalière [local_date, local_date+1) plutôt que
    sur l'égalité stricte (les timestamps stockés ont une heure).
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
    ).scalar_one_or_none()


def _persist_brief(
    brief_json: dict,
    now_local: datetime,
    *,
    synthesis_failed: bool = False,
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
) -> dict:
    """Envoie email + WhatsApp. Met à jour Brief.delivery_status en conséquence.

    Si `revision > 1`, le sujet email mentionne "Révision N" et une bannière
    est affichée en tête pour que le lecteur sache que c'est une mise à jour.

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
            revision=revision,
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
