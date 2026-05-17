"""Tests du parser PDF BRVM.

Stratégie : tests unitaires purs + tests d'intégration sur 2 PDFs réels
téléchargés depuis `brvm.org` (layouts 2021 et 2026) pour garantir la
robustesse face aux changements de format.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# --- Pure helpers -----------------------------------------------------------

@pytest.mark.unit
class TestParseFrNumber:
    def test_space_thousands_comma_decimal(self):
        from src.backfill.brvm_pdf import _parse_fr_number

        assert _parse_fr_number("12 400") == 12400.0
        assert _parse_fr_number("12 400,50") == pytest.approx(12400.50)
        assert _parse_fr_number("3 595 695") == 3595695.0

    def test_percent_suffix(self):
        from src.backfill.brvm_pdf import _parse_fr_number

        assert _parse_fr_number("5,82 %") == pytest.approx(5.82)
        assert _parse_fr_number("-0,87 %") == pytest.approx(-0.87)
        assert _parse_fr_number("0,00 %") == 0.0

    def test_en_format(self):
        from src.backfill.brvm_pdf import _parse_fr_number

        assert _parse_fr_number("1,418,335.00") == pytest.approx(1418335.0)
        assert _parse_fr_number("3.31") == pytest.approx(3.31)

    def test_parentheses_negative(self):
        from src.backfill.brvm_pdf import _parse_fr_number

        assert _parse_fr_number("(1 234)") == -1234.0

    def test_invalid_returns_none(self):
        from src.backfill.brvm_pdf import _parse_fr_number

        assert _parse_fr_number("N/A") is None
        assert _parse_fr_number("") is None
        assert _parse_fr_number(None) is None


@pytest.mark.unit
class TestDateExtraction:
    def test_text_fr_day_of_week_format(self):
        """Format dominant : 'N° 251 jeudi 30 décembre 2021'."""
        from src.backfill.brvm_pdf import _extract_date_from_text

        text = "BULLETIN... N° 251 jeudi 30 décembre 2021 Site : www.brvm.org"
        dt = _extract_date_from_text(text)
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2021, 12, 30)

    def test_text_seance_du(self):
        from src.backfill.brvm_pdf import _extract_date_from_text

        dt = _extract_date_from_text("Séance du 15 janvier 2024")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2024, 1, 15)

    def test_text_numeric_fallback(self):
        from src.backfill.brvm_pdf import _extract_date_from_text

        dt = _extract_date_from_text("Bulletin au 30/12/2022")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2022, 12, 30)

    def test_text_rejects_historic_dates(self):
        """Borne : BRVM créée en 1998, pas de date antérieure → fallback None."""
        from src.backfill.brvm_pdf import _extract_date_from_text

        # "01/01/1990" → rejeté par la sanity check
        assert _extract_date_from_text("date perdue 01/01/1990") is None

    def test_filename_compact_yyyymmdd(self):
        from src.backfill.brvm_pdf import _extract_date_from_filename

        dt = _extract_date_from_filename("boc_20260423_2.pdf")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 4, 23)

    def test_filename_fr_date(self):
        from src.backfill.brvm_pdf import _extract_date_from_filename

        dt = _extract_date_from_filename("boc_du_15_01_2024.pdf")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2024, 1, 15)

    def test_filename_iso(self):
        from src.backfill.brvm_pdf import _extract_date_from_filename

        dt = _extract_date_from_filename("bulletin-2024-01-15.pdf")
        assert dt is not None
        assert dt.year == 2024 and dt.month == 1 and dt.day == 15

    def test_filename_unrecognized_returns_none(self):
        from src.backfill.brvm_pdf import _extract_date_from_filename

        assert _extract_date_from_filename("random_document.pdf") is None
        assert _extract_date_from_filename("") is None


@pytest.mark.unit
class TestTickerDetection:
    def test_detect_ticker_at_col_0_old_format(self):
        """Pré-2023 : ticker en col 0, nom en col 1."""
        from src.backfill.brvm_pdf import _detect_ticker_column

        row = ["SNTS", "SONATEL", "", "14 200", "14 250", "14 250", "0,35 %"]
        assert _detect_ticker_column(row) == 0

    def test_detect_ticker_at_col_1_new_format(self):
        """2023+ : code sectoriel en col 0, ticker en col 1."""
        from src.backfill.brvm_pdf import _detect_ticker_column

        row = ["CB", "NTLC", "NESTLE CI", "", "12 400", "12 400", "12 400"]
        assert _detect_ticker_column(row) == 1

    def test_rejects_unknown_tickers(self):
        from src.backfill.brvm_pdf import _detect_ticker_column

        row = ["foo", "bar", "baz"]
        assert _detect_ticker_column(row) is None

    def test_empty_row_returns_none(self):
        from src.backfill.brvm_pdf import _detect_ticker_column

        assert _detect_ticker_column([]) is None
        assert _detect_ticker_column(["", "", ""]) is None


@pytest.mark.unit
class TestParseQuotationRow:
    def test_old_format_ticker_col_0(self):
        """2021 : 15 colonnes, ticker en col 0."""
        from src.backfill.brvm_pdf import BrvmPdfResult, _parse_quotation_row

        row = ["CABC", "SICABLE CI", "", "1 010", "1 080", "1 010", "0,00 %",
               "400", "408 250", "1 010", "0,00 %", "133,00", "2-août-21",
               "13,17 %", "5,13"]
        result = BrvmPdfResult(quote_date=None)
        _parse_quotation_row(row, ticker_col=0, row_num=0, result=result)

        assert len(result.quotes) == 1
        q = result.quotes[0]
        assert q.ticker == "CABC"
        assert q.close_price == 1010.0
        assert q.variation_pct == 0.0
        assert q.volume == 400
        assert q.value_traded == 408250.0
        assert q.open_price == 1080.0
        assert q.previous_close == 1010.0

    def test_new_format_ticker_col_1(self):
        """2026 : 16 colonnes, code sectoriel en col 0."""
        from src.backfill.brvm_pdf import BrvmPdfResult, _parse_quotation_row

        row = ["CB", "NTLC", "NESTLE CI", "", "12 400", "12 400", "12 400",
               "0,00 %", "293", "3 595 695", "12 400", "16,43 %", "721,6",
               "18-août-25", "5,82 %", "15,08"]
        result = BrvmPdfResult(quote_date=None)
        _parse_quotation_row(row, ticker_col=1, row_num=0, result=result)

        assert len(result.quotes) == 1
        q = result.quotes[0]
        assert q.ticker == "NTLC"
        assert q.close_price == 12400.0
        assert q.volume == 293
        assert q.value_traded == 3595695.0

    def test_skips_rows_without_close(self):
        """Lignes de séparateur / total → skip silencieux (pas d'erreur)."""
        from src.backfill.brvm_pdf import BrvmPdfResult, _parse_quotation_row

        row = ["SNTS", "SONATEL", "", "", "", "", "", "", "", "", "", "", "", "", ""]
        result = BrvmPdfResult(quote_date=None)
        _parse_quotation_row(row, ticker_col=0, row_num=0, result=result)
        assert len(result.quotes) == 0
        assert len(result.errors) == 0


@pytest.mark.unit
class TestParseBrvmPdfRejection:
    def test_empty_bytes_produces_error(self):
        from src.backfill.brvm_pdf import parse_brvm_pdf

        result = parse_brvm_pdf(b"", filename="x.pdf")
        assert result.quotes == []
        assert len(result.errors) > 0

    def test_invalid_bytes_produces_error(self):
        from src.backfill.brvm_pdf import parse_brvm_pdf

        result = parse_brvm_pdf(b"not a pdf at all", filename="x.pdf")
        assert result.quotes == []
        assert len(result.errors) > 0


# --- Integration tests on real PDFs ----------------------------------------

# Fixtures téléchargées depuis brvm.org (voir README fixtures).

@pytest.mark.integration
class TestRealBrvmPdf:
    """Exécuté uniquement si les fixtures sont présentes — regression test
    contre une évolution accidentelle du parser qui casserait sur du vrai data."""

    def _read(self, name: str) -> bytes | None:
        p = FIXTURES / name
        if not p.exists():
            return None
        return p.read_bytes()

    def test_2026_format_new_layout(self):
        """Bulletin du 23/04/2026 — 47 cotations, layout 16 colonnes avec code sectoriel."""
        from src.backfill.brvm_pdf import parse_brvm_pdf

        content = self._read("boc_20260423_2.pdf")
        if content is None:
            pytest.skip("Fixture boc_20260423_2.pdf absente")

        result = parse_brvm_pdf(content, filename="boc_20260423_2.pdf")
        assert result.quote_date == datetime(2026, 4, 23, tzinfo=UTC)
        # BRVM cote 45-50 titres à tout moment — on accepte ± 5 pour absorber
        # l'évolution de la liste (nouvelle cotation, radiation).
        assert 40 <= len(result.quotes) <= 60, (
            f"Nombre de cotations inattendu : {len(result.quotes)}"
        )
        # Quelques tickers qu'on veut absolument voir
        tickers = {q.ticker for q in result.quotes}
        for must_have in {"NTLC", "SNTS", "BOAC", "SPHC", "PALC"}:
            assert must_have in tickers, f"{must_have} manquant dans {tickers}"

        # Vérif cohérence des prix : tous les closes > 0 et < 1M FCFA
        for q in result.quotes:
            assert 1 <= q.close_price <= 1_000_000, (
                f"Prix improbable pour {q.ticker}: {q.close_price}"
            )
            assert q.volume >= 0
            assert q.value_traded >= 0

    def test_2021_format_old_layout(self):
        """Bulletin du 30/12/2021 — 15 colonnes, ticker en col 0 (pré-2023)."""
        from src.backfill.brvm_pdf import parse_brvm_pdf

        content = self._read("boc_20211230_2.pdf")
        if content is None:
            pytest.skip("Fixture boc_20211230_2.pdf absente")

        result = parse_brvm_pdf(content, filename="boc_20211230_2.pdf")
        assert result.quote_date == datetime(2021, 12, 30, tzinfo=UTC)
        assert 40 <= len(result.quotes) <= 60

        # NESTLE coté ~4665 en décembre 2021 — valeur canonique pour détecter
        # une régression de mapping de colonnes.
        ntlc = next((q for q in result.quotes if q.ticker == "NTLC"), None)
        assert ntlc is not None, "NTLC manquant"
        assert 4000 <= ntlc.close_price <= 5500, (
            f"Close NTLC 2021 improbable : {ntlc.close_price}"
        )
