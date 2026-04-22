"""Q-1 : A/B test synthèse Opus vs Sonnet.

Vérifie que :
- Quand `ab_test_synthesis=False` (défaut) : l'appel alt n'est pas fait.
- Quand activé mais modèle alt == principal : skip (pas de double appel inutile).
- Quand activé et modèle distinct : l'appel alt est fait, payload_alt passé à _persist_brief.
- Si l'appel alt plante : le brief principal est quand même livré normalement.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestAbTestDispatch:
    def _base_patches(self, mock_collect, mock_persist_coll, mock_enrich,
                      mock_snapshot, mock_history, mock_ticker_fund,
                      mock_persist_brief, mock_deliver):
        mock_collect.return_value = []
        mock_persist_coll.return_value = ([], {})
        mock_enrich.return_value = []
        mock_snapshot.return_value = {"quotes_count": 10}
        mock_history.return_value = []
        mock_ticker_fund.return_value = []
        mock_persist_brief.return_value = (42, 1)
        mock_deliver.return_value = {"email": True, "whatsapp": False}

    @patch("src.pipeline._deliver")
    @patch("src.pipeline._persist_brief")
    @patch("src.pipeline._build_ticker_fundamentals")
    @patch("src.pipeline._build_historical_context")
    @patch("src.pipeline._build_market_snapshot")
    @patch("src.pipeline._enrich_news")
    @patch("src.pipeline._persist_collection")
    @patch("src.pipeline._collect_all")
    @patch("src.pipeline.BriefSynthesizer")
    def test_ab_disabled_does_not_call_alt(
        self, mock_synth_cls, mock_collect, mock_persist_coll, mock_enrich,
        mock_snapshot, mock_history, mock_ticker_fund, mock_persist_brief,
        mock_deliver, monkeypatch,
    ):
        from src import pipeline
        from src.config import get_settings

        monkeypatch.setenv("AB_TEST_SYNTHESIS", "false")
        get_settings.cache_clear()

        self._base_patches(mock_collect, mock_persist_coll, mock_enrich,
                           mock_snapshot, mock_history, mock_ticker_fund,
                           mock_persist_brief, mock_deliver)
        mock_synth_cls.return_value.synthesize.return_value = {
            "market_summary": "OK", "opportunities": [],
        }

        pipeline._run_pipeline_body(run_id=1)

        # Le synthétiseur n'est instancié qu'une seule fois (principal)
        assert mock_synth_cls.call_count == 1
        # payload_alt pas passé
        kwargs = mock_persist_brief.call_args.kwargs
        assert kwargs.get("payload_alt") is None
        assert kwargs.get("model_alt") is None

    @patch("src.pipeline._deliver")
    @patch("src.pipeline._persist_brief")
    @patch("src.pipeline._build_ticker_fundamentals")
    @patch("src.pipeline._build_historical_context")
    @patch("src.pipeline._build_market_snapshot")
    @patch("src.pipeline._enrich_news")
    @patch("src.pipeline._persist_collection")
    @patch("src.pipeline._collect_all")
    @patch("src.pipeline.BriefSynthesizer")
    def test_ab_enabled_calls_alt_model(
        self, mock_synth_cls, mock_collect, mock_persist_coll, mock_enrich,
        mock_snapshot, mock_history, mock_ticker_fund, mock_persist_brief,
        mock_deliver, monkeypatch,
    ):
        from src import pipeline
        from src.config import get_settings

        monkeypatch.setenv("AB_TEST_SYNTHESIS", "true")
        monkeypatch.setenv("AB_TEST_MODEL", "claude-sonnet-4-6")
        monkeypatch.setenv("MODEL_SYNTHESIS", "claude-opus-4-7")
        get_settings.cache_clear()

        self._base_patches(mock_collect, mock_persist_coll, mock_enrich,
                           mock_snapshot, mock_history, mock_ticker_fund,
                           mock_persist_brief, mock_deliver)
        primary = {"market_summary": "opus", "opportunities": []}
        alt = {"market_summary": "sonnet", "opportunities": []}
        mock_synth_cls.return_value.synthesize.side_effect = [primary, alt]

        pipeline._run_pipeline_body(run_id=2)

        # 2 instances : principal sans model override, alt avec model="claude-sonnet-4-6"
        assert mock_synth_cls.call_count == 2
        # 2e instance créée avec le modèle alt
        second_call = mock_synth_cls.call_args_list[1]
        assert second_call.kwargs.get("model") == "claude-sonnet-4-6"

        # payload_alt propagé à _persist_brief
        kwargs = mock_persist_brief.call_args.kwargs
        assert kwargs.get("payload_alt") == alt
        assert kwargs.get("model_alt") == "claude-sonnet-4-6"

    @patch("src.pipeline._deliver")
    @patch("src.pipeline._persist_brief")
    @patch("src.pipeline._build_ticker_fundamentals")
    @patch("src.pipeline._build_historical_context")
    @patch("src.pipeline._build_market_snapshot")
    @patch("src.pipeline._enrich_news")
    @patch("src.pipeline._persist_collection")
    @patch("src.pipeline._collect_all")
    @patch("src.pipeline.BriefSynthesizer")
    def test_ab_skips_when_models_identical(
        self, mock_synth_cls, mock_collect, mock_persist_coll, mock_enrich,
        mock_snapshot, mock_history, mock_ticker_fund, mock_persist_brief,
        mock_deliver, monkeypatch,
    ):
        """Si MODEL_SYNTHESIS == AB_TEST_MODEL on n'appelle pas 2x le même."""
        from src import pipeline
        from src.config import get_settings

        monkeypatch.setenv("AB_TEST_SYNTHESIS", "true")
        monkeypatch.setenv("AB_TEST_MODEL", "claude-opus-4-7")
        monkeypatch.setenv("MODEL_SYNTHESIS", "claude-opus-4-7")
        get_settings.cache_clear()

        self._base_patches(mock_collect, mock_persist_coll, mock_enrich,
                           mock_snapshot, mock_history, mock_ticker_fund,
                           mock_persist_brief, mock_deliver)
        mock_synth_cls.return_value.synthesize.return_value = {
            "market_summary": "OK", "opportunities": [],
        }

        pipeline._run_pipeline_body(run_id=3)

        assert mock_synth_cls.call_count == 1
        assert mock_persist_brief.call_args.kwargs.get("payload_alt") is None

    @patch("src.pipeline._deliver")
    @patch("src.pipeline._persist_brief")
    @patch("src.pipeline._build_ticker_fundamentals")
    @patch("src.pipeline._build_historical_context")
    @patch("src.pipeline._build_market_snapshot")
    @patch("src.pipeline._enrich_news")
    @patch("src.pipeline._persist_collection")
    @patch("src.pipeline._collect_all")
    @patch("src.pipeline.BriefSynthesizer")
    def test_alt_failure_does_not_block_primary(
        self, mock_synth_cls, mock_collect, mock_persist_coll, mock_enrich,
        mock_snapshot, mock_history, mock_ticker_fund, mock_persist_brief,
        mock_deliver, monkeypatch,
    ):
        from src import pipeline
        from src.config import get_settings

        monkeypatch.setenv("AB_TEST_SYNTHESIS", "true")
        monkeypatch.setenv("AB_TEST_MODEL", "claude-sonnet-4-6")
        monkeypatch.setenv("MODEL_SYNTHESIS", "claude-opus-4-7")
        get_settings.cache_clear()

        self._base_patches(mock_collect, mock_persist_coll, mock_enrich,
                           mock_snapshot, mock_history, mock_ticker_fund,
                           mock_persist_brief, mock_deliver)
        primary = {"market_summary": "opus", "opportunities": []}
        mock_synth_cls.return_value.synthesize.side_effect = [
            primary,
            RuntimeError("boom sonnet"),
        ]

        pipeline._run_pipeline_body(run_id=4)

        # Delivery quand même appelé (primaire n'a pas planté)
        mock_deliver.assert_called_once()
        kwargs = mock_persist_brief.call_args.kwargs
        # payload_alt=None car l'appel alt a planté
        assert kwargs.get("payload_alt") is None
