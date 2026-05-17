"""Tests unitaires du service backfill — helpers purs sans DB."""
from __future__ import annotations

import pytest


@pytest.mark.unit
class TestGuessTickerFromFilename:
    def test_simple_ticker_prefix(self):
        from src.backfill.service import _guess_ticker_from_filename

        assert _guess_ticker_from_filename("SNTS.csv") == "SNTS"
        assert _guess_ticker_from_filename("SNTS_history.csv") == "SNTS"
        assert _guess_ticker_from_filename("snts-2024.csv") == "SNTS"
        assert _guess_ticker_from_filename("BOAC.csv") == "BOAC"

    def test_case_insensitive(self):
        from src.backfill.service import _guess_ticker_from_filename

        assert _guess_ticker_from_filename("snts_history.csv") == "SNTS"

    def test_no_match_returns_none(self):
        from src.backfill.service import _guess_ticker_from_filename

        # Trop court
        assert _guess_ticker_from_filename("x.csv") is None
        # Commence par chiffre → pas un ticker
        assert _guess_ticker_from_filename("2024_data.csv") is None
        # Vide
        assert _guess_ticker_from_filename("") is None

    def test_strips_extension(self):
        from src.backfill.service import _guess_ticker_from_filename

        assert _guess_ticker_from_filename("SNTS.csv") == "SNTS"
        # Extension absente — le ticker doit être suivi d'un séparateur OU
        # être l'ensemble du nom.
        assert _guess_ticker_from_filename("SNTS") == "SNTS"

    def test_preserves_multi_char_tickers(self):
        from src.backfill.service import _guess_ticker_from_filename

        # BOABF (Bank of Africa Burkina Faso) — 5 chars
        assert _guess_ticker_from_filename("BOABF_hist.csv") == "BOABF"


@pytest.mark.unit
class TestSanitizeFilename:
    def test_removes_special_chars(self):
        from src.backfill.service import _sanitize_filename

        assert _sanitize_filename("foo bar.pdf") == "foo_bar.pdf"
        assert _sanitize_filename("foo@bar/baz.pdf") == "foo_bar_baz.pdf"

    def test_caps_length(self):
        from src.backfill.service import _sanitize_filename

        long_name = "a" * 500 + ".pdf"
        result = _sanitize_filename(long_name)
        assert len(result) <= 200

    def test_empty_returns_fallback(self):
        from src.backfill.service import _sanitize_filename

        assert _sanitize_filename("") == "unnamed"


@pytest.mark.unit
class TestStorageKeyFormat:
    def test_combines_job_item_filename(self):
        from src.backfill.service import _storage_key_for

        assert _storage_key_for(42, 7, "SNTS.csv") == "42/7/SNTS.csv"
        # Sanitization appliquée
        assert _storage_key_for(1, 1, "bad name!.pdf") == "1/1/bad_name_.pdf"
