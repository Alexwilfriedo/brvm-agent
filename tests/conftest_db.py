"""Extensions conftest pour tests integration — DB + mocks LLM + mocks delivery.

Importer dans ``tests/conftest.py`` via::

    from .conftest_db import *  # noqa: F401,F403

Prérequis : ``pip install testcontainers[postgresql] respx httpx pytest-asyncio``
et Docker disponible localement (sinon fixture ``pg_container`` skip).

Ces fixtures matérialisent la stratégie du test-design : on exécute les tests
integration sur un Postgres réel (pas SQLite) pour couvrir les comportements
spécifiques (advisory lock, JSONB, `ON CONFLICT`), et on mocke systématiquement
Anthropic + Brevo pour éviter tout appel réseau.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# --- Postgres real container -------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Postgres 16 éphémère via testcontainers. Skip si Docker absent."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers[postgresql] non installé — tests integration skippés")

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def _engine(pg_container):
    """Engine SQLAlchemy pointant sur le container, schéma initialisé."""
    import os

    os.environ["DATABASE_URL"] = pg_container.get_connection_url()

    # Rebuild engine avec la vraie DB_URL du container
    from src import database

    database.engine.dispose()
    database.engine = database._make_engine()  # type: ignore[attr-defined]
    database.init_db()
    return database.engine


@pytest.fixture
def db_session(_engine) -> "Iterator[Session]":
    """Session avec rollback automatique en fin de test (isolation)."""
    from sqlalchemy.orm import Session

    connection = _engine.connect()
    trans = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


# --- Claude API mocks --------------------------------------------------------


@pytest.fixture
def claude_enrichment_ok():
    """Mock respx : Sonnet enrichment retourne un payload réaliste."""
    import respx
    from httpx import Response

    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(
            return_value=Response(
                200,
                json={
                    "id": "msg_test",
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '{"tickers_mentioned": ["SNTS"], '
                                '"enrichment": {"sentiment": "positive", '
                                '"materiality": "high", "summary": "Résultats Q1 SONATEL en hausse."}}'
                            ),
                        }
                    ],
                    "usage": {"input_tokens": 300, "output_tokens": 80},
                },
            )
        )
        yield mock


@pytest.fixture
def claude_synthesis_ok():
    """Mock respx : Opus synthesis retourne un brief JSON conforme."""
    import respx
    from httpx import Response

    payload = {
        "summary": "Séance positive sur la BRVM. SONATEL mène les hausses.",
        "key_news": [
            {"title": "Résultats Q1 SONATEL", "url": "https://example.ci/news/1", "ticker": "SNTS"}
        ],
        "opportunities": [
            {
                "ticker": "SNTS",
                "direction": "buy",
                "conviction": 4,
                "thesis": "Momentum positif post-résultats, volume en hausse.",
                "price_at_signal": 14250,
                "horizon": "short_term",
            }
        ],
        "alerts": [],
    }

    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(
            return_value=Response(
                200,
                json={
                    "id": "msg_opus",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": __import__("json").dumps(payload)}],
                    "usage": {"input_tokens": 1200, "output_tokens": 500},
                },
            )
        )
        yield mock


@pytest.fixture
def claude_down():
    """Mock respx : Anthropic retourne 503 en boucle (simule panne)."""
    import respx
    from httpx import Response

    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(return_value=Response(503, json={"error": "overloaded"}))
        yield mock


# --- Brevo mocks -------------------------------------------------------------


@pytest.fixture
def smtp_mock(monkeypatch):
    """Mock smtplib.SMTP : capture les emails envoyés en mémoire."""
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    yield sent


@pytest.fixture
def wassoya_ok():
    """Mock respx : Wassoya retourne 200 OK."""
    import respx
    from httpx import Response

    with respx.mock(base_url="https://api.wassoya.com") as mock:
        mock.post("/messages").mock(return_value=Response(200, json={"status": "sent"}))
        yield mock


# --- Sample data fixtures ----------------------------------------------------


@pytest.fixture
def sample_quotes():
    """20 quotes BRVM réalistes pour step 2/4 tests."""
    from datetime import date

    tickers = [
        ("SNTS", "SONATEL", 14250, 2.3, 12500),
        ("ETIT", "ECOBANK", 8900, -0.5, 3400),
        ("BOAC", "BOA CI", 5600, 1.1, 800),
        ("SGBC", "SGB CI", 12400, 0.0, 200),
        ("NTLC", "NESTLE CI", 75000, -1.2, 50),
        # ... compléter au besoin
    ]
    return [
        {
            "ticker": t,
            "name": n,
            "close_price": p,
            "variation_pct": v,
            "volume": vol,
            "quote_date": date.today(),
        }
        for (t, n, p, v, vol) in tickers
    ]


@pytest.fixture
def sample_news_articles():
    """Articles news pour step 2/3 tests."""
    from datetime import UTC, datetime

    return [
        {
            "url": "https://sika-finance.com/article/snts-q1",
            "title": "SONATEL publie des résultats Q1 en hausse",
            "source_key": "sika-finance",
            "published_at": datetime.now(UTC),
            "summary": "Le groupe télécom affiche +8% de CA.",
            "content": "Longtemps contenu...",
        },
        {
            "url": "https://rfi-afrique.fr/ivoire/budget-2026",
            "title": "Côte d'Ivoire : budget 2026 en débat",
            "source_key": "rfi-afrique",
            "published_at": datetime.now(UTC),
            "summary": "Focus infrastructures.",
            "content": "...",
        },
    ]
