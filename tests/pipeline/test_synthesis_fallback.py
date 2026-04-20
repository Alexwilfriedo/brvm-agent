"""D-5 : si la synthèse Opus échoue (payload stub `_error=True`),
le pipeline DOIT :
- persister le brief pour forensic (avec delivery_status="failed_synth")
- NE PAS appeler `_deliver` (pas d'email, pas de WhatsApp)
- marquer `pipeline_runs.status="failed_synthesis"`
- NE PAS créer de signals

Ces tests isolent la logique de branchement dans `_run_pipeline_body` sans
dépendre de Postgres (les étapes DB sont mockées via `unittest.mock`).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestSynthesisFailureSkipsDelivery:
    """Vérifie le contrat D-5 via les patches des fonctions étape."""

    def _stub_payload(self, reason: str = "anthropic: 503 overloaded") -> dict:
        """Réplique de `synthesis._error_payload` pour ne pas importer l'interne."""
        return {
            "market_summary": "Erreur de génération du brief.",
            "opportunities": [],
            "alerts": [f"Synthèse indisponible : {reason[:200]}"],
            "skip_reasons": reason,
            "_error": True,
            "_raw_preview": "",
        }

    @patch("src.pipeline._deliver")
    @patch("src.pipeline._persist_brief")
    @patch("src.pipeline._build_historical_context")
    @patch("src.pipeline._build_market_snapshot")
    @patch("src.pipeline._enrich_news")
    @patch("src.pipeline._persist_collection")
    @patch("src.pipeline._collect_all")
    @patch("src.pipeline.BriefSynthesizer")
    def test_stub_payload_skips_delivery_and_marks_status(
        self,
        mock_synth_cls,
        mock_collect,
        mock_persist_coll,
        mock_enrich,
        mock_snapshot,
        mock_history,
        mock_persist_brief,
        mock_deliver,
    ):
        # Arrange — chaque étape renvoie une valeur neutre, sauf la synthèse
        # qui renvoie un payload stub.
        from src import pipeline

        mock_collect.return_value = []
        mock_persist_coll.return_value = []
        mock_enrich.return_value = []
        mock_snapshot.return_value = {"quotes_count": 10}
        mock_history.return_value = []
        mock_synth_cls.return_value.synthesize.return_value = self._stub_payload()
        mock_persist_brief.return_value = (42, 1)  # brief_id, revision

        # Act
        summary = pipeline._run_pipeline_body(run_id=1)

        # Assert — livraison JAMAIS appelée
        mock_deliver.assert_not_called()

        # Le brief est persisté avec le flag explicite
        mock_persist_brief.assert_called_once()
        kwargs = mock_persist_brief.call_args.kwargs
        assert kwargs.get("synthesis_failed") is True, (
            "persist_brief doit recevoir synthesis_failed=True pour marquer le brief"
        )

        # Summary propage le marker que run_daily_pipeline utilise
        assert summary.get("synthesis_failed") is True
        assert summary["brief_id"] == 42

        # Step deliver marqué comme skipped
        deliver_step = next(
            (s for s in summary["steps"] if s.get("step") == "deliver"), None
        )
        assert deliver_step is not None
        assert deliver_step.get("skipped") is True
        assert deliver_step.get("reason") == "synthesis_failed"

    @patch("src.pipeline._deliver")
    @patch("src.pipeline._persist_brief")
    @patch("src.pipeline._build_historical_context")
    @patch("src.pipeline._build_market_snapshot")
    @patch("src.pipeline._enrich_news")
    @patch("src.pipeline._persist_collection")
    @patch("src.pipeline._collect_all")
    @patch("src.pipeline.BriefSynthesizer")
    def test_happy_path_still_delivers(
        self,
        mock_synth_cls,
        mock_collect,
        mock_persist_coll,
        mock_enrich,
        mock_snapshot,
        mock_history,
        mock_persist_brief,
        mock_deliver,
    ):
        """Contre-test : sans `_error`, la livraison DOIT être appelée normalement."""
        from src import pipeline

        mock_collect.return_value = []
        mock_persist_coll.return_value = []
        mock_enrich.return_value = []
        mock_snapshot.return_value = {"quotes_count": 10}
        mock_history.return_value = []
        mock_synth_cls.return_value.synthesize.return_value = {
            "market_summary": "OK",
            "opportunities": [{"ticker": "SNTS", "direction": "buy", "conviction": 4,
                               "thesis": "momentum"}],
        }
        mock_persist_brief.return_value = (42, 1)
        mock_deliver.return_value = {"email": True, "whatsapp": False}

        summary = pipeline._run_pipeline_body(run_id=2)

        mock_deliver.assert_called_once()
        assert summary.get("synthesis_failed") is not True
        assert mock_persist_brief.call_args.kwargs.get("synthesis_failed") is False


@pytest.mark.unit
class TestRunDailyPipelineStatus:
    """Le status final du run doit refléter la réalité utilisateur :
    'failed_synthesis' si aucun brief n'a été livré, pas 'success'."""

    @patch("src.pipeline._end_run")
    @patch("src.pipeline._run_pipeline_body")
    @patch("src.pipeline._pipeline_lock")
    @patch("src.pipeline._start_run")
    @patch("src.pipeline._find_brief_for_date")
    @patch("src.pipeline.get_session")
    def test_status_is_failed_synthesis_when_stub(
        self,
        mock_session,
        mock_find,
        mock_start_run,
        mock_lock,
        mock_body,
        mock_end_run,
    ):
        from src import pipeline

        # No existing brief for today → don't short-circuit
        mock_find.return_value = None
        mock_session.return_value.__enter__.return_value = MagicMock()

        mock_start_run.return_value = 99
        mock_lock.return_value.__enter__.return_value = True
        mock_body.return_value = {
            "brief_id": 42,
            "synthesis_failed": True,
            "steps": [],
        }

        result = pipeline.run_daily_pipeline(trigger="cron", force=True)

        # Le status final DOIT être "failed_synthesis", pas "success"
        mock_end_run.assert_called_once()
        call_kwargs = mock_end_run.call_args.kwargs
        assert call_kwargs["status"] == "failed_synthesis"
        assert result["status"] == "failed_synthesis"

    @patch("src.pipeline._end_run")
    @patch("src.pipeline._run_pipeline_body")
    @patch("src.pipeline._pipeline_lock")
    @patch("src.pipeline._start_run")
    @patch("src.pipeline._find_brief_for_date")
    @patch("src.pipeline.get_session")
    def test_status_is_success_on_happy_path(
        self,
        mock_session,
        mock_find,
        mock_start_run,
        mock_lock,
        mock_body,
        mock_end_run,
    ):
        from src import pipeline

        mock_find.return_value = None
        mock_session.return_value.__enter__.return_value = MagicMock()
        mock_start_run.return_value = 100
        mock_lock.return_value.__enter__.return_value = True
        mock_body.return_value = {"brief_id": 43, "steps": []}

        result = pipeline.run_daily_pipeline(trigger="cron", force=True)

        call_kwargs = mock_end_run.call_args.kwargs
        assert call_kwargs["status"] == "success"
        assert result["status"] == "success"
