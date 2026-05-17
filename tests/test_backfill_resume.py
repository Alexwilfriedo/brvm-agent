"""Tests ciblés sur `resume_job` — transitions d'état (M4).

Teste les règles de transition sans DB réelle : on passe un objet mock
minimal qui émule `session.get()` avec les attributs requis.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeSession:
    """Session minimaliste : `.get(Model, pk)` retourne l'objet qu'on lui a
    passé au constructeur. `execute(...)` retourne un objet vide pour les
    helpers de resume (qui ne checkent les items pending que si status=paused)."""

    def __init__(self, job):
        self._job = job

    def get(self, model, pk):
        from src.models import BackfillJob
        if model is BackfillJob and self._job.id == pk:
            return self._job
        return None

    def execute(self, stmt):
        # Pour tester le chemin `status=paused`, on simule qu'il reste 1 item
        # pending. Les tests qui exercent d'autres chemins (running+pause) ne
        # touchent pas execute().
        return SimpleNamespace(scalar_one_or_none=lambda: 1)


def _make_job(**overrides):
    """Fabrique un BackfillJob minimal avec valeurs par défaut safe."""
    from src.models import BackfillJob
    defaults = {
        "id": 1,
        "status": "paused",
        "source_type": "pdf_brvm",
        "total_items": 10,
        "processed_items": 3,
        "failed_items": 0,
        "inserted_quotes": 0,
        "updated_quotes": 0,
        "pause_requested": False,
        "requested_by": None,
        "message": None,
    }
    defaults.update(overrides)
    return BackfillJob(**defaults)


@pytest.mark.unit
class TestResumeJob:
    def test_resume_from_paused_transitions_to_running(self):
        from src.backfill.service import resume_job

        job = _make_job(status="paused", pause_requested=False)
        s = _FakeSession(job)
        out = resume_job(s, 1)
        assert out is job
        assert out.status == "running"
        assert out.pause_requested is False

    def test_resume_in_flight_pause_clears_flag_keeps_running(self):
        """M4 : pause demandée mais pas encore appliquée → clear le flag sans
        casser la course en cours."""
        from src.backfill.service import resume_job

        job = _make_job(status="running", pause_requested=True)
        s = _FakeSession(job)
        out = resume_job(s, 1)
        assert out.status == "running"
        assert out.pause_requested is False
        assert "annulée" in (out.message or "").lower()

    def test_resume_running_without_pause_flag_refuses(self):
        """Un job sain en cours ne doit pas pouvoir être 'resumé' (pas de raison)."""
        from src.backfill.service import BackfillError, resume_job

        job = _make_job(status="running", pause_requested=False)
        s = _FakeSession(job)
        with pytest.raises(BackfillError):
            resume_job(s, 1)

    def test_resume_completed_refuses(self):
        from src.backfill.service import BackfillError, resume_job

        job = _make_job(status="completed", pause_requested=False)
        s = _FakeSession(job)
        with pytest.raises(BackfillError):
            resume_job(s, 1)

    def test_resume_cancelled_refuses(self):
        from src.backfill.service import BackfillError, resume_job

        job = _make_job(status="cancelled", pause_requested=False)
        s = _FakeSession(job)
        with pytest.raises(BackfillError):
            resume_job(s, 1)

    def test_resume_nonexistent_raises_not_found(self):
        from src.backfill.service import JobNotFoundError, resume_job

        job = _make_job(id=999)
        s = _FakeSession(job)
        with pytest.raises(JobNotFoundError):
            resume_job(s, 1)  # id 1 vs 999 → not found
