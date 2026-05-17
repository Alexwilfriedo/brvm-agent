"""Tests unitaires pour le router `api/analyze.py`.

Même philosophie que `test_trades.py` : on teste les schémas Pydantic et les
helpers purs sans lancer un vrai serveur FastAPI ni toucher la DB. Les tests
d'intégration DB (testcontainers) sont hors scope v1.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError


@pytest.mark.unit
class TestInvestmentAnalysisCreate:
    def test_normalizes_ticker_uppercase(self):
        from src.api.analyze import InvestmentAnalysisCreate

        payload = InvestmentAnalysisCreate(ticker=" snts ", horizon="medium")
        assert payload.ticker == "SNTS"

    def test_rejects_unknown_horizon(self):
        from src.api.analyze import InvestmentAnalysisCreate

        with pytest.raises(ValidationError):
            InvestmentAnalysisCreate(ticker="SNTS", horizon="forever")

    def test_rejects_ticker_too_short(self):
        from src.api.analyze import InvestmentAnalysisCreate

        with pytest.raises(ValidationError):
            InvestmentAnalysisCreate(ticker="S", horizon="short")

    def test_rejects_ticker_too_long(self):
        from src.api.analyze import InvestmentAnalysisCreate

        with pytest.raises(ValidationError):
            InvestmentAnalysisCreate(ticker="A" * 20, horizon="short")

    def test_accepts_all_three_horizons(self):
        from src.api.analyze import InvestmentAnalysisCreate

        for h in ("short", "medium", "long"):
            payload = InvestmentAnalysisCreate(ticker="SNTS", horizon=h)
            assert payload.horizon == h


@pytest.mark.unit
class TestAgeMinutes:
    def test_recent_returns_small_value(self):
        from src.api.analyze import _age_minutes

        now = datetime.now(UTC)
        assert _age_minutes(now - timedelta(minutes=5)) == pytest.approx(5, abs=0.1)

    def test_naive_datetime_treated_as_utc(self):
        """Défense : même si une datetime naïve remonte, on ne crashe pas."""
        from src.api.analyze import _age_minutes

        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=3)
        # Ne doit pas lever (normalement tz-aware mais on tolère)
        assert _age_minutes(naive) == pytest.approx(3, abs=0.1)


@pytest.mark.unit
class TestCacheTTLContract:
    """Vérifie que la constante _CACHE_TTL_MINUTES est dans une plage raisonnable.

    Garde-fou contre une modif accidentelle qui ferait exploser le budget Opus
    (TTL = 0 = jamais de cache) ou qui figerait les analyses trop longtemps
    (TTL = 1 jour = on ne voit plus les nouvelles news).
    """

    def test_ttl_is_reasonable(self):
        from src.api.analyze import _CACHE_TTL_MINUTES

        assert 5 <= _CACHE_TTL_MINUTES <= 120, (
            f"_CACHE_TTL_MINUTES={_CACHE_TTL_MINUTES} hors plage raisonnable "
            "[5, 120] — risque budget Opus ou analyses obsolètes."
        )
