"""Tests unitaires du schéma TradeCreate (M-1)."""
import pytest
from pydantic import ValidationError


@pytest.mark.unit
class TestTradeCreate:
    def test_normalizes_ticker_uppercase(self):
        from src.api.trades import TradeCreate

        t = TradeCreate(ticker=" snts ", action="buy", quantity=100, unit_price=14250)
        assert t.ticker == "SNTS"

    def test_rejects_negative_quantity(self):
        from src.api.trades import TradeCreate

        with pytest.raises(ValidationError, match="greater than 0"):
            TradeCreate(ticker="SNTS", action="buy", quantity=0, unit_price=14250)

    def test_rejects_negative_price(self):
        from src.api.trades import TradeCreate

        with pytest.raises(ValidationError, match="greater than 0"):
            TradeCreate(ticker="SNTS", action="buy", quantity=1, unit_price=0)

    def test_rejects_invalid_action(self):
        from src.api.trades import TradeCreate

        with pytest.raises(ValidationError):
            TradeCreate(ticker="SNTS", action="hold", quantity=1, unit_price=14250)

    def test_rejects_invalid_reason(self):
        from src.api.trades import TradeCreate

        with pytest.raises(ValidationError):
            TradeCreate(
                ticker="SNTS", action="buy", quantity=1, unit_price=14250,
                reason="gut-feeling",
            )

    def test_defaults_reason_to_other(self):
        from src.api.trades import TradeCreate

        t = TradeCreate(ticker="SNTS", action="buy", quantity=1, unit_price=14250)
        assert t.reason == "other"

    def test_rejects_ticker_too_short(self):
        from src.api.trades import TradeCreate

        with pytest.raises(ValidationError, match="at least 2 characters"):
            TradeCreate(ticker="X", action="buy", quantity=1, unit_price=14250)
