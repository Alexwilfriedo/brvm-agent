"""Tests unitaires pour `analysis/investment_advisor.py`.

Focus sur la logique pure (sanitizer, coercion, degraded payload). Les tests
d'intégration qui exercent le vrai flux DB + Anthropic vivent dans
`tests/api/test_analyze.py`.
"""
from __future__ import annotations

import pytest


@pytest.mark.unit
class TestCoerceRecommendation:
    def test_buy_normalized(self):
        from src.analysis.investment_advisor import _coerce_recommendation

        assert _coerce_recommendation("buy") == "buy"
        assert _coerce_recommendation(" BUY ") == "buy"
        assert _coerce_recommendation("Acheter") == "buy"
        assert _coerce_recommendation("achat") == "buy"

    def test_avoid_normalized(self):
        from src.analysis.investment_advisor import _coerce_recommendation

        assert _coerce_recommendation("avoid") == "avoid"
        assert _coerce_recommendation("sell") == "avoid"
        assert _coerce_recommendation("éviter") == "avoid"
        assert _coerce_recommendation("vendre") == "avoid"

    def test_unknown_falls_back_to_hold(self):
        from src.analysis.investment_advisor import _coerce_recommendation

        assert _coerce_recommendation("maybe") == "hold"
        assert _coerce_recommendation("") == "hold"
        assert _coerce_recommendation(None) == "hold"
        assert _coerce_recommendation(42) == "hold"


@pytest.mark.unit
class TestClampConfidence:
    def test_within_range(self):
        from src.analysis.investment_advisor import _clamp_confidence

        assert _clamp_confidence(0.5) == 0.5
        assert _clamp_confidence(0.0) == 0.0
        assert _clamp_confidence(1.0) == 1.0

    def test_clamped(self):
        from src.analysis.investment_advisor import _clamp_confidence

        assert _clamp_confidence(1.5) == 1.0
        assert _clamp_confidence(-0.2) == 0.0

    def test_invalid_falls_back_to_zero(self):
        from src.analysis.investment_advisor import _clamp_confidence

        assert _clamp_confidence(None) == 0.0
        assert _clamp_confidence("high") == 0.0


@pytest.mark.unit
class TestClampHorizonDays:
    def test_short_window(self):
        from src.analysis.investment_advisor import _clamp_horizon_days

        assert _clamp_horizon_days(5, horizon="short") == 5
        # Hors borne inférieure → ramené à 3
        assert _clamp_horizon_days(1, horizon="short") == 3
        # Hors borne supérieure → ramené à 15
        assert _clamp_horizon_days(100, horizon="short") == 15

    def test_medium_window(self):
        from src.analysis.investment_advisor import _clamp_horizon_days

        assert _clamp_horizon_days(60, horizon="medium") == 60
        assert _clamp_horizon_days(5, horizon="medium") == 16
        assert _clamp_horizon_days(500, horizon="medium") == 90

    def test_long_window(self):
        from src.analysis.investment_advisor import _clamp_horizon_days

        assert _clamp_horizon_days(200, horizon="long") == 200
        assert _clamp_horizon_days(10, horizon="long") == 91
        assert _clamp_horizon_days(1000, horizon="long") == 365

    def test_none_passthrough(self):
        from src.analysis.investment_advisor import _clamp_horizon_days

        assert _clamp_horizon_days(None, horizon="medium") is None


@pytest.mark.unit
class TestSanitizePrices:
    """Anti-hallucination : corrige les prix aberrants renvoyés par Opus."""

    def test_forces_price_at_analysis_to_db_close(self):
        """Si Opus renvoie un close différent du DB, on force au DB."""
        from src.analysis.investment_advisor import _sanitize_prices

        data = {
            "recommendation": "buy",
            "price_at_analysis": 12000.0,  # Opus a halluciné
            "price_target": 14000.0,
        }
        out = _sanitize_prices(
            data,
            current_price=15000.0,  # vrai close DB
            horizon="medium",
            model="claude-opus-4-7",
            ticker="SNTS",
        )
        assert out["price_at_analysis"] == 15000.0
        assert "_sanitize_issues" in out

    def test_keeps_price_at_analysis_if_aligned(self):
        """Tolérance 0.5% — pas de correction si Opus cite bien le close."""
        from src.analysis.investment_advisor import _sanitize_prices

        data = {"price_at_analysis": 15000.0, "recommendation": "hold"}
        out = _sanitize_prices(
            data,
            current_price=15000.0,
            horizon="medium",
            model="claude-opus-4-7",
            ticker="SNTS",
        )
        assert out["price_at_analysis"] == 15000.0
        assert "_sanitize_issues" not in out

    def test_neutralizes_target_above_2x(self):
        """price_target > 2× current → None (Opus a perdu l'ancrage)."""
        from src.analysis.investment_advisor import _sanitize_prices

        data = {
            "recommendation": "buy",
            "price_at_analysis": 10000.0,
            "price_target": 25000.0,  # 2.5× — absurde
        }
        out = _sanitize_prices(
            data,
            current_price=10000.0,
            horizon="long",
            model="claude-opus-4-7",
            ticker="X",
        )
        assert out["price_target"] is None
        assert "_sanitize_issues" in out

    def test_neutralizes_target_below_half(self):
        from src.analysis.investment_advisor import _sanitize_prices

        data = {
            "recommendation": "avoid",
            "price_at_analysis": 10000.0,
            "price_target": 4000.0,  # 0.4× — absurde
        }
        out = _sanitize_prices(
            data,
            current_price=10000.0,
            horizon="long",
            model="claude-opus-4-7",
            ticker="X",
        )
        assert out["price_target"] is None

    def test_keeps_target_within_bounds(self):
        from src.analysis.investment_advisor import _sanitize_prices

        data = {
            "recommendation": "buy",
            "price_at_analysis": 10000.0,
            "price_target": 12000.0,  # 1.2× — normal
        }
        out = _sanitize_prices(
            data,
            current_price=10000.0,
            horizon="medium",
            model="claude-opus-4-7",
            ticker="X",
        )
        assert out["price_target"] == 12000.0
        assert "_sanitize_issues" not in out

    def test_neutralizes_stop_loss_above_current_on_buy(self):
        """Un stop-loss au-dessus du prix d'entrée sur un buy = incohérent."""
        from src.analysis.investment_advisor import _sanitize_prices

        data = {
            "recommendation": "buy",
            "price_at_analysis": 10000.0,
            "stop_loss": 11000.0,
        }
        out = _sanitize_prices(
            data,
            current_price=10000.0,
            horizon="medium",
            model="claude-opus-4-7",
            ticker="X",
        )
        assert out["stop_loss"] is None

    def test_handles_missing_fields_gracefully(self):
        """Opus peut omettre des champs — sanitize ne doit jamais crasher."""
        from src.analysis.investment_advisor import _sanitize_prices

        out = _sanitize_prices(
            {"recommendation": "hold"},
            current_price=10000.0,
            horizon="short",
            model="claude-opus-4-7",
            ticker="X",
        )
        # price_at_analysis vide → on injecte le current_price
        assert out["price_at_analysis"] == 10000.0

    def test_non_dict_returns_error(self):
        from src.analysis.investment_advisor import _sanitize_prices

        out = _sanitize_prices(
            "not a dict",  # type: ignore[arg-type]
            current_price=10000.0,
            horizon="short",
            model="claude-opus-4-7",
            ticker="X",
        )
        assert out == {"_error": "opus_returned_non_dict"}


@pytest.mark.unit
class TestDegradedResult:
    """Fallback quand Opus a échoué — toujours un résultat exploitable."""

    def test_returns_hold_with_error_flag(self):
        from src.analysis.investment_advisor import _degraded_result

        res = _degraded_result(
            reason="timeout",
            model="claude-opus-4-7",
            price_at_analysis=15000.0,
        )
        assert res.recommendation == "hold"
        assert res.confidence == 0.0
        assert res.price_at_analysis == 15000.0
        assert res.price_target is None
        assert res.payload["_error"] is True
        assert "timeout" in res.payload["_error_reason"]

    def test_preserves_token_counts_on_invalid_json(self):
        """Si on a quand même consommé des tokens avant de se planter sur le
        JSON, on les remonte pour que la facture reste visible."""
        from src.analysis.investment_advisor import _degraded_result

        res = _degraded_result(
            reason="invalid_json",
            model="claude-opus-4-7",
            price_at_analysis=15000.0,
            input_tokens=1200,
            output_tokens=80,
        )
        assert res.input_tokens == 1200
        assert res.output_tokens == 80
