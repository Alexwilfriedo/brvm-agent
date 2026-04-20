"""Initialisation de l'observabilité : logs structurés + Sentry.

Logs :
  - dev (`LOG_LEVEL=DEBUG`) → format humain coloré
  - prod → JSON (pour ingestion par Railway / Grafana / Datadog)

Sentry :
  - activé seulement si `SENTRY_DSN` est défini
  - filtre les routes `/health` (bruit)
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure `logging` stdlib + `structlog` pour produire des logs cohérents."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    is_prod = settings.log_level.upper() == "INFO"
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if is_prod
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def configure_sentry(settings: Settings) -> bool:
    """Init Sentry si `SENTRY_DSN` est défini. Retourne True si activé."""
    if not settings.sentry_dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    def _before_send(event: dict, hint: dict) -> dict | None:
        # Ignore les requêtes healthcheck (bruit)
        req = event.get("request", {})
        if req.get("url", "").endswith("/health"):
            return None
        return event

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
        ],
        before_send=_before_send,
        send_default_pii=False,
    )
    return True
