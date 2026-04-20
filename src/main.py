"""Application FastAPI — point d'entrée Railway.

Démarre le scheduler dans un `lifespan` et expose les endpoints admin pour
configurer cron/sources et consulter les briefs.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from .api import briefs, preview, runs, schedule, sources
from .api import recipients as recipients_api
from .collectors.registry import DEFAULT_SOURCES
from .config import get_settings
from .database import get_session, init_db
from .models import Recipient, Source
from .observability import configure_logging, configure_sentry
from .scheduler import get_scheduler

settings = get_settings()
configure_logging(settings)
_sentry_enabled = configure_sentry(settings)

logger = logging.getLogger(__name__)


def _seed_sources_if_empty() -> None:
    """Si la table sources est vide, insère les sources par défaut."""
    with get_session() as s:
        existing = s.execute(select(Source).limit(1)).scalars().first()
        if existing is not None:
            return
        logger.info("Seeding des sources par défaut…")
        for src in DEFAULT_SOURCES:
            s.add(Source(**src))


def _seed_recipients_from_env() -> None:
    """Au 1er boot : si `recipients` est vide ET que `.env` contient `EMAIL_TO`
    ou `WHATSAPP_TO_NUMBER`, crée les recipients correspondants.

    Ensuite, l'API admin (/api/recipients) prend la main — ces env vars
    ne sont plus lues. C'est uniquement pour onboarding zero-click.
    """
    with get_session() as s:
        existing = s.execute(select(Recipient).limit(1)).scalars().first()
        if existing is not None:
            return
        seeded = 0
        if settings.email_to:
            s.add(Recipient(
                channel="email",
                address=settings.email_to,
                name=None,
                notes="Seed initial depuis EMAIL_TO",
            ))
            seeded += 1
        if settings.whatsapp_to_number:
            s.add(Recipient(
                channel="whatsapp",
                address=settings.whatsapp_to_number,
                name=None,
                notes="Seed initial depuis WHATSAPP_TO_NUMBER",
            ))
            seeded += 1
        if seeded:
            logger.info(f"Seeding de {seeded} recipient(s) depuis .env")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup : init DB + seed + scheduler. Shutdown : stop scheduler."""
    logger.info("=== Démarrage BRVM Agent ===")
    if _sentry_enabled:
        logger.info("Sentry activé (env=%s)", settings.sentry_environment)

    init_db()
    _seed_sources_if_empty()
    _seed_recipients_from_env()

    scheduler = get_scheduler()
    scheduler.start()

    try:
        yield
    finally:
        logger.info("=== Arrêt BRVM Agent ===")
        scheduler.shutdown()


app = FastAPI(
    title="BRVM Agent",
    description="Agent de veille et d'analyse BRVM — brief quotidien automatisé.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(sources.router)
app.include_router(schedule.router)
app.include_router(briefs.router)
app.include_router(runs.router)
app.include_router(recipients_api.router)
app.include_router(preview.router)


@app.get("/health", tags=["health"])
def health():
    """Healthcheck Railway — NE PAS protéger par auth (Railway doit y accéder)."""
    scheduler = get_scheduler()
    job = (
        scheduler.scheduler.get_job("daily_brief")
        if scheduler.scheduler.running
        else None
    )
    return {
        "status": "ok",
        "scheduler_running": scheduler.scheduler.running,
        "next_run": str(job.next_run_time) if job else None,
        "sentry": _sentry_enabled,
    }


@app.get("/", tags=["root"])
def root():
    return {
        "name": "BRVM Agent",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }
