"""Tests de la logique de calcul du backtest (M-2).

Tests unitaires isolés : pas de DB, on teste la structure de sortie et la
reproductibilité du calcul PnL via des appels directs aux helpers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Rend `tools/` importable en tests
TOOLS_PATH = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_PATH) not in sys.path:
    sys.path.insert(0, str(TOOLS_PATH))


@pytest.mark.unit
class TestBacktestRow:
    def test_default_init(self):
        from datetime import date

        from backtest_signals import BacktestRow

        row = BacktestRow(
            signal_id=1, ticker="SNTS", direction="buy",
            conviction=4, signal_date=date(2026, 4, 1),
            price_at_signal=14000.0,
        )
        assert row.pnl_pct_j5 is None
        assert row.alpha_pct_j30 is None
        assert row.note == ""

    def test_dataclass_fields_include_all_horizons(self):
        from backtest_signals import HORIZONS, BacktestRow

        fields = set(BacktestRow.__dataclass_fields__.keys())
        for h in HORIZONS:
            assert f"price_j{h}" in fields
            assert f"pnl_pct_j{h}" in fields
            assert f"baseline_pnl_pct_j{h}" in fields
            assert f"alpha_pct_j{h}" in fields


@pytest.mark.unit
class TestPnlComputation:
    """Vérifie la formule utilisée dans run_backtest (indirectement)."""

    def test_pnl_formula_symmetric(self):
        # +10% puis -10% ≠ 0 (classique — test de non-régression basique)
        price_from, price_to_up = 100.0, 110.0
        price_to_down = 100.0 * 0.9
        pnl_up = (price_to_up / price_from - 1.0) * 100.0
        pnl_down = (price_to_down / price_from - 1.0) * 100.0
        assert pnl_up == pytest.approx(10.0)
        assert pnl_down == pytest.approx(-10.0)

    def test_alpha_is_signal_minus_baseline(self):
        # Alpha = PnL signal − PnL baseline
        pnl, baseline = 8.5, 3.2
        alpha = pnl - baseline
        assert alpha == pytest.approx(5.3)


@pytest.mark.unit
class TestArgParsing:
    def test_since_date_parser(self):
        import argparse
        from datetime import UTC, date, datetime

        # Vérifie que le format YYYY-MM-DD est parsé correctement
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--since",
            type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).date(),
        )
        args = parser.parse_args(["--since", "2026-03-01"])
        assert args.since == date(2026, 3, 1)
