"""Tests du parser CSV d'historique de cotations."""
from __future__ import annotations

import pytest


@pytest.mark.unit
class TestParseStandardCsv:
    def test_iso_dates_comma_delimiter_dot_decimal(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = (
            "date,open,high,low,close,volume\n"
            "2024-01-15,14200,14300,14150,14250,12500\n"
            "2024-01-16,14250,14400,14200,14380,8000\n"
        )
        r = parse_historical_csv(csv)
        assert len(r.rows) == 2
        assert r.rows[0].quote_date.year == 2024
        assert r.rows[0].close_price == 14250.0
        assert r.rows[0].open_price == 14200.0
        assert r.rows[0].volume == 12500
        assert r.detected_delimiter == ","

    def test_semicolon_delimiter_with_comma_decimal(self):
        """Export Excel FR typique : ';' et décimale ','."""
        from src.collectors.historical_import import parse_historical_csv

        csv = (
            "Date;Clôture;Volume\n"
            "15/01/2024;14250,50;12 500\n"
            "16/01/2024;14380,75;8000\n"
        )
        r = parse_historical_csv(csv)
        assert len(r.rows) == 2
        assert r.rows[0].close_price == pytest.approx(14250.50)
        assert r.rows[0].volume == 12500
        assert r.detected_delimiter == ";"
        assert r.detected_columns["close"] == "Clôture"

    def test_tab_delimiter(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "date\tclose\tvolume\n2024-01-15\t14250\t12500\n"
        r = parse_historical_csv(csv)
        assert len(r.rows) == 1
        assert r.detected_delimiter == "\t"


@pytest.mark.unit
class TestDateFormats:
    def test_fr_date(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "date,close\n15/01/2024,14250\n"
        r = parse_historical_csv(csv)
        assert r.rows[0].quote_date.day == 15
        assert r.rows[0].quote_date.month == 1

    def test_fr_short_year(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "date,close\n15/01/24,14250\n"
        r = parse_historical_csv(csv)
        assert r.rows[0].quote_date.year == 2024

    def test_compact_yyyymmdd(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "date,close\n20240115,14250\n"
        r = parse_historical_csv(csv)
        assert r.rows[0].quote_date.day == 15

    def test_invalid_date_is_skipped_not_raised(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = (
            "date,close\n"
            "not-a-date,14250\n"
            "2024-01-15,14300\n"
        )
        r = parse_historical_csv(csv)
        assert len(r.rows) == 1
        assert r.skipped == 1
        assert any("date invalide" in e for e in r.errors)


@pytest.mark.unit
class TestHeaderAliases:
    def test_french_accented_headers(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "Séance,Cours\n2024-01-15,14250\n"
        r = parse_historical_csv(csv)
        assert len(r.rows) == 1

    def test_english_alt_headers(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "Timestamp,Last,Shares\n2024-01-15,14250,500\n"
        r = parse_historical_csv(csv)
        assert len(r.rows) == 1
        assert r.rows[0].close_price == 14250.0
        assert r.rows[0].volume == 500

    def test_extra_columns_are_ignored(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = (
            "date,close,some_metric,another,ticker\n"
            "2024-01-15,14250,xxx,yyy,SNTS\n"
        )
        r = parse_historical_csv(csv)
        assert len(r.rows) == 1
        assert r.rows[0].close_price == 14250.0


@pytest.mark.unit
class TestErrorCases:
    def test_empty_file_raises(self):
        from src.collectors.historical_import import ImportCsvError, parse_historical_csv

        with pytest.raises(ImportCsvError, match="vide"):
            parse_historical_csv("")

    def test_missing_close_raises(self):
        from src.collectors.historical_import import ImportCsvError, parse_historical_csv

        with pytest.raises(ImportCsvError, match="close"):
            parse_historical_csv("date,volume\n2024-01-15,500\n")

    def test_missing_date_raises(self):
        from src.collectors.historical_import import ImportCsvError, parse_historical_csv

        with pytest.raises(ImportCsvError, match="date"):
            parse_historical_csv("close,volume\n14250,500\n")

    def test_strict_mode_raises_on_first_bad_row(self):
        from src.collectors.historical_import import ImportCsvError, parse_historical_csv

        csv = (
            "date,close\n"
            "bad,14250\n"
        )
        with pytest.raises(ImportCsvError):
            parse_historical_csv(csv, strict=True)

    def test_negative_close_is_skipped(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = (
            "date,close\n"
            "2024-01-15,-100\n"
            "2024-01-16,14200\n"
        )
        r = parse_historical_csv(csv)
        assert len(r.rows) == 1
        assert r.skipped == 1


@pytest.mark.unit
class TestEncoding:
    def test_utf8_bom_tolerated(self):
        from src.collectors.historical_import import parse_historical_csv

        csv_bytes = "﻿date,close\n2024-01-15,14250\n".encode("utf-8")
        r = parse_historical_csv(csv_bytes)
        assert len(r.rows) == 1

    def test_latin1_fallback(self):
        """Export Windows Excel FR avec accents encodés en Latin-1."""
        from src.collectors.historical_import import parse_historical_csv

        # "Clôture" en Latin-1
        csv_bytes = b"date;Cl\xf4ture\n2024-01-15;14250\n"
        r = parse_historical_csv(csv_bytes)
        assert len(r.rows) == 1


@pytest.mark.unit
class TestDedup:
    def test_same_date_appears_once(self):
        """Si le CSV contient 2 lignes pour la même date, on garde la 1ère."""
        from src.collectors.historical_import import parse_historical_csv

        csv = (
            "date,close\n"
            "2024-01-15,14250\n"
            "2024-01-15,14999\n"
            "2024-01-16,14300\n"
        )
        r = parse_historical_csv(csv)
        assert len(r.rows) == 2
        assert r.rows[0].close_price == 14250  # 1ère gagne
        assert r.skipped == 1


@pytest.mark.unit
class TestVariationPct:
    def test_pct_suffix_stripped(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "date,close,variation\n2024-01-15,14250,+1.5%\n"
        r = parse_historical_csv(csv)
        assert r.rows[0].variation_pct == pytest.approx(1.5)

    def test_negative_variation(self):
        from src.collectors.historical_import import parse_historical_csv

        csv = "date,close,var\n2024-01-15,14250,-2.3%\n"
        r = parse_historical_csv(csv)
        assert r.rows[0].variation_pct == pytest.approx(-2.3)


@pytest.mark.unit
class TestParenthesesAccounting:
    """Convention comptable : (123) = -123."""

    def test_parentheses_as_negative(self):
        from src.collectors.historical_import import _parse_float

        assert _parse_float("(1500.50)") == -1500.50

    def test_thousands_separator_fr(self):
        from src.collectors.historical_import import _parse_float

        assert _parse_float("1 234,56", decimal_is_comma=True) == pytest.approx(1234.56)

    def test_thousands_separator_en(self):
        from src.collectors.historical_import import _parse_float

        assert _parse_float("1,234.56", decimal_is_comma=False) == pytest.approx(1234.56)


@pytest.mark.unit
class TestKnownTickersGuard:
    """M1 : l'endpoint /import refuse un ticker absent du référentiel BRVM."""

    def test_known_tickers_contains_major_brvm_codes(self):
        """Sanity check : les tickers majeurs qu'on doit pouvoir importer sont
        bien dans le set utilisé pour la validation."""
        from src.api.market import _KNOWN_TICKERS

        for major in {"SNTS", "NTLC", "BOAC", "PALC", "SPHC"}:
            assert major in _KNOWN_TICKERS, f"{major} manquant du référentiel"

    def test_fake_ticker_not_in_known_set(self):
        from src.api.market import _KNOWN_TICKERS

        assert "FAKE" not in _KNOWN_TICKERS
        assert "XYZ" not in _KNOWN_TICKERS
