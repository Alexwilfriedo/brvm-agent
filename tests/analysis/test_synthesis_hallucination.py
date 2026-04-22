"""A-5 : le filtre anti-hallucination retire les opportunities dont le
ticker n'apparaît dans aucune donnée d'entrée (snapshot, news, fundamentals)."""
from __future__ import annotations

import pytest


@pytest.mark.unit
class TestCollectInputTickers:
    def test_gathers_from_all_sources(self):
        from src.analysis.synthesis import _collect_input_tickers

        snapshot = {
            "top_gainers": [{"ticker": "SNTS", "var_pct": 3.2}],
            "top_losers": [{"ticker": "ETIT"}],
            "top_volumes": [{"ticker": "BOAC"}],
        }
        news = [
            {"tickers_mentioned": ["NTLC", "SGBC"]},
            {"enrichment": {"tickers_mentioned": ["PALC"]}},
        ]
        fundamentals = [{"ticker": "SMBC"}]

        known = _collect_input_tickers(snapshot, news, fundamentals)
        assert known == {"SNTS", "ETIT", "BOAC", "NTLC", "SGBC", "PALC", "SMBC"}

    def test_case_insensitive_and_trimmed(self):
        from src.analysis.synthesis import _collect_input_tickers

        snapshot = {"top_gainers": [{"ticker": "  snts "}, {"ticker": "etit"}]}
        known = _collect_input_tickers(snapshot, [], [])
        assert known == {"SNTS", "ETIT"}

    def test_handles_missing_or_malformed(self):
        from src.analysis.synthesis import _collect_input_tickers

        # Ne doit pas crasher sur des structures partielles
        known = _collect_input_tickers(
            {"top_gainers": [{"no_ticker": "x"}, "not-a-dict"]},
            [{"tickers_mentioned": None}, "not-a-dict"],
            [],
        )
        assert known == set()


@pytest.mark.unit
class TestHallucinationFilter:
    def _snapshot_with(self, *tickers: str) -> dict:
        return {"top_gainers": [{"ticker": t} for t in tickers]}

    def test_keeps_legitimate_opportunities(self):
        from src.analysis.synthesis import _filter_hallucinated_tickers

        data = {
            "opportunities": [
                {"ticker": "SNTS", "direction": "buy"},
                {"ticker": "ETIT", "direction": "watch"},
            ],
        }
        out = _filter_hallucinated_tickers(
            data,
            market_snapshot=self._snapshot_with("SNTS", "ETIT"),
            enriched_news=[],
            ticker_fundamentals=[],
            model="claude-opus-4-7",
        )
        assert len(out["opportunities"]) == 2
        assert "_hallucination_filter" not in out

    def test_drops_unknown_ticker(self):
        from src.analysis.synthesis import _filter_hallucinated_tickers

        data = {
            "opportunities": [
                {"ticker": "SNTS", "direction": "buy"},
                {"ticker": "FAKE", "direction": "buy"},  # hallucination
                {"ticker": "ETIT", "direction": "watch"},
            ],
        }
        out = _filter_hallucinated_tickers(
            data,
            market_snapshot=self._snapshot_with("SNTS", "ETIT"),
            enriched_news=[],
            ticker_fundamentals=[],
            model="claude-opus-4-7",
        )
        kept_tickers = [o["ticker"] for o in out["opportunities"]]
        assert kept_tickers == ["SNTS", "ETIT"]
        # Trace dans le payload
        assert out["_hallucination_filter"]["dropped"] == ["FAKE"]
        assert out["_hallucination_filter"]["dropped_count"] == 1

    def test_case_insensitive_matching(self):
        from src.analysis.synthesis import _filter_hallucinated_tickers

        data = {"opportunities": [{"ticker": "snts"}]}
        out = _filter_hallucinated_tickers(
            data,
            market_snapshot=self._snapshot_with("SNTS"),
            enriched_news=[],
            ticker_fundamentals=[],
            model="claude-opus-4-7",
        )
        assert len(out["opportunities"]) == 1

    def test_skips_validation_when_no_known_tickers(self):
        """Cas boot initial : pas de quote en DB → on ne filtre rien pour
        éviter de vider tous les briefs du jour."""
        from src.analysis.synthesis import _filter_hallucinated_tickers

        data = {"opportunities": [{"ticker": "ANYTHING"}]}
        out = _filter_hallucinated_tickers(
            data,
            market_snapshot={"top_gainers": []},
            enriched_news=[],
            ticker_fundamentals=[],
            model="claude-opus-4-7",
        )
        assert len(out["opportunities"]) == 1

    def test_no_opportunities_is_noop(self):
        from src.analysis.synthesis import _filter_hallucinated_tickers

        data = {"market_summary": "rien à signaler", "opportunities": []}
        out = _filter_hallucinated_tickers(
            data,
            market_snapshot=self._snapshot_with("SNTS"),
            enriched_news=[],
            ticker_fundamentals=[],
            model="claude-opus-4-7",
        )
        assert out == data

    def test_fundamentals_source_legitimizes_ticker(self):
        from src.analysis.synthesis import _filter_hallucinated_tickers

        # Ticker absent du snapshot mais présent dans les fundamentals fournis
        # à Opus → légitime.
        data = {"opportunities": [{"ticker": "NTLC"}]}
        out = _filter_hallucinated_tickers(
            data,
            market_snapshot=self._snapshot_with("SNTS"),
            enriched_news=[],
            ticker_fundamentals=[{"ticker": "NTLC", "close_price": 75000}],
            model="claude-opus-4-7",
        )
        assert len(out["opportunities"]) == 1
        assert "_hallucination_filter" not in out


@pytest.mark.unit
class TestCaptureLlmError:
    """C-4 : la capture Sentry est silencieuse quand Sentry n'est pas actif."""

    def test_does_not_raise_without_sentry(self, monkeypatch):
        # Simule l'absence de sentry_sdk en faisant lever ImportError
        import builtins

        from src.analysis.enrichment import _capture_llm_error
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sentry_sdk":
                raise ImportError("sentry_sdk not available")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        # Ne doit pas planter
        _capture_llm_error(
            RuntimeError("boom"), step="enrich", model="test-model",
            article_url="https://x",
        )
