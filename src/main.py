"""Application FastAPI — point d'entrée Railway.

Démarre le scheduler dans un `lifespan` et expose les endpoints admin pour
configurer cron/sources et consulter les briefs.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from . import events as event_bus
from .api import auth as auth_api
from .api import briefs, market, preview, runs, schedule, sources, stats, users
from .api import recipients as recipients_api
from .collectors.registry import DEFAULT_SOURCES
from .config import get_settings
from .database import get_session, init_db
from .models import Recipient, Source, User
from .observability import configure_logging, configure_sentry
from .scheduler import get_scheduler

settings = get_settings()
configure_logging(settings)
_sentry_enabled = configure_sentry(settings)

logger = logging.getLogger(__name__)


def _seed_sources_if_empty() -> None:
    """Seed des sources par défaut — insère les `key` manquantes.

    Idempotent : une source déjà présente n'est pas écrasée (config/enabled
    préservés). Permet d'ajouter de nouvelles sources via release sans
    toucher aux existantes.
    """
    with get_session() as s:
        existing_keys = {k for (k,) in s.execute(select(Source.key)).all()}
        missing = [src for src in DEFAULT_SOURCES if src["key"] not in existing_keys]
        if not missing:
            return
        if not existing_keys:
            logger.info(f"Seeding des {len(missing)} sources par défaut…")
        else:
            keys = ", ".join(src["key"] for src in missing)
            logger.info(f"Ajout de {len(missing)} nouvelle(s) source(s) : {keys}")
        for src in missing:
            s.add(Source(**src))


def _seed_initial_admin() -> None:
    """Si `users` est vide ET `INITIAL_ADMIN_EMAIL` est défini, crée le 1er user."""
    email = (settings.initial_admin_email or "").lower().strip()
    if not email:
        return
    with get_session() as s:
        existing = s.execute(select(User).limit(1)).scalars().first()
        if existing is not None:
            return
        logger.info(f"Seeding du 1er admin : {email}")
        s.add(User(email=email, name=None, enabled=True))


def _reap_orphan_runs() -> None:
    """Marque `failed` tout PipelineRun resté à `status=running` au boot.

    Un run à status=running au démarrage = crash précédent (uvicorn reload,
    OOM, SIGTERM) qui a tué le thread du pipeline avant que `_end_run` ne soit
    exécuté. L'advisory lock Postgres a été libéré en fermant la connexion,
    mais la ligne DB est restée orpheline. Sans ce reaper, elle apparaît
    "En cours" pour toujours dans l'UI.
    """
    from datetime import UTC, datetime
    from .models import PipelineRun
    with get_session() as s:
        orphans = s.execute(
            select(PipelineRun).where(PipelineRun.status == "running")
        ).scalars().all()
        if not orphans:
            return
        now = datetime.now(UTC)
        for run in orphans:
            run.status = "failed"
            run.ended_at = now
            run.error = "Run orphelin — process tué avant la fin (reap au boot)."
            run.summary = {**(run.summary or {}), "reaped_at": now.isoformat()}
        logger.info(f"Reaped {len(orphans)} run(s) orphelin(s) au boot")


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
    _seed_initial_admin()
    _reap_orphan_runs()

    scheduler = get_scheduler()
    scheduler.start()

    try:
        yield
    finally:
        logger.info("=== Arrêt BRVM Agent ===")
        scheduler.shutdown()
        event_bus.shutdown()  # annule les timers de purge SSE en vol


# Swagger `/docs` désactivé en prod — infos-leak mineur mais zero coût à fermer.
_is_prod = settings.sentry_environment.lower() in {"production", "prod"}
app = FastAPI(
    title="BRVM Agent",
    description="Agent de veille et d'analyse BRVM — brief quotidien automatisé.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# CORS — autorise la console admin (liste d'origines depuis CORS_ORIGINS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,  # pas de cookies — auth via header Bearer / X-Admin-Token
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token", "Authorization"],
    expose_headers=[],
    max_age=600,
)

app.include_router(auth_api.router)
app.include_router(sources.router)
app.include_router(schedule.router)
app.include_router(briefs.router)
app.include_router(runs.router)
app.include_router(runs.stream_router)  # /api/runs/:id/stream (auth via query token)
app.include_router(recipients_api.router)
app.include_router(users.router)
app.include_router(market.router)
app.include_router(stats.router)
app.include_router(preview.router)


@app.get("/health", tags=["health"])
def health():
    """Healthcheck Railway — NE PAS protéger par auth (Railway doit y accéder).

    Ne renvoie que le strict nécessaire : pas de scheduler state (info-leak),
    Railway n'a besoin que du 200 OK.
    """
    return {"status": "ok"}


@app.get("/", tags=["root"])
def root():
    return {"name": "BRVM Agent", "version": "0.1.0"}
