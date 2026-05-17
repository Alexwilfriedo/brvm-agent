"""Endpoints pour le backfill d'historique reprisable.

Auth : admin (JWT ou X-Admin-Token). Traçabilité via `requested_by`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..backfill import runner, service
from ..backfill.service import BackfillError, JobNotFoundError
from ..database import get_session
from ..models import BackfillItem, BackfillJob
from ..storage import StorageNotConfigured
from .auth import UserOut, current_user
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, MAX_LIMIT, PaginatedResponse, paginate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/backfill",
    tags=["backfill"],
    dependencies=[Depends(require_admin)],
)

# Cap sur l'upload agrégé — 1200 bulletins × 100 KB ≈ 120 MB, on cap à 200 MB
# pour se laisser une marge. Au-delà, l'utilisateur doit splitter en plusieurs
# jobs (le système est conçu pour).
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Cap nombre d'items par job — évite les uploads accidentels catastrophiques.
_MAX_ITEMS_PER_JOB = 2000


# --- Schemas ----------------------------------------------------------------

SourceType = Literal["pdf_brvm", "csv"]


class BackfillItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    kind: str
    status: str
    ticker_hint: str | None
    inserted_quotes: int
    updated_quotes: int
    error: str | None
    meta: dict
    processed_at: datetime | None


class BackfillJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    source_type: str
    total_items: int
    processed_items: int
    failed_items: int
    inserted_quotes: int
    updated_quotes: int
    pause_requested: bool
    requested_by: str | None
    message: str | None
    created_at: datetime
    started_at: datetime | None
    paused_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class BackfillJobDetailOut(BackfillJobOut):
    items: list[BackfillItemOut]


# --- Routes -----------------------------------------------------------------

@router.post(
    "/jobs",
    response_model=BackfillJobOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_backfill_job(
    user: Annotated[UserOut, Depends(current_user)],
    source_type: SourceType = Form(..., description="pdf_brvm ou csv"),
    files: list[UploadFile] = File(..., description="Fichiers à importer"),
) -> BackfillJobOut:
    """Crée un job + upload des fichiers en un seul call multipart.

    Le worker démarre immédiatement et traite les items en FIFO. Le client
    doit ensuite poller `GET /api/backfill/jobs/{id}` pour suivre la progression.

    Args:
      - **source_type** : `pdf_brvm` pour des bulletins BRVM PDF, `csv` pour
        des historiques par ticker (le nom du fichier doit commencer par le
        ticker, ex: `SNTS_history.csv`).
      - **files** : entre 1 et 2000 fichiers, volume total ≤ 200 MB.
    """
    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier fourni.")
    if len(files) > _MAX_ITEMS_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trop de fichiers ({len(files)}). Cap : {_MAX_ITEMS_PER_JOB}. "
                "Splitter en plusieurs jobs."
            ),
        )

    # Lecture binaire de tous les fichiers — borné par _MAX_UPLOAD_BYTES.
    total_size = 0
    file_tuples: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        total_size += len(content)
        if total_size > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload trop gros (> {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB). "
                    "Splitter en plusieurs jobs."
                ),
            )
        file_tuples.append((f.filename or "unnamed", content))

    with get_session() as s:
        try:
            job = service.create_job(
                s,
                source_type=source_type,
                files=file_tuples,
                requested_by=user.email if user else None,
            )
        except StorageNotConfigured as e:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Object storage non configuré : {e}. Configure S3_BUCKET + "
                    "credentials (S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY) et "
                    "S3_ENDPOINT_URL pour MinIO."
                ),
            ) from e
        except BackfillError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        s.flush()
        job_dict = _serialize_job(job)

    # Wake-up / démarre le worker (idempotent).
    runner.ensure_running()
    return BackfillJobOut(**job_dict)


@router.get("/jobs", response_model=PaginatedResponse[BackfillJobOut])
def list_backfill_jobs(
    status_filter: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
):
    """Liste paginée des jobs, triés par plus récent d'abord."""
    limit = min(max(1, limit), MAX_LIMIT)
    offset = max(0, offset)
    with get_session() as s:
        stmt = select(BackfillJob).order_by(BackfillJob.created_at.desc())
        if status_filter:
            stmt = stmt.where(BackfillJob.status == status_filter)
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[BackfillJobOut](
            items=[BackfillJobOut(**_serialize_job(j)) for j in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/jobs/{job_id}", response_model=BackfillJobDetailOut)
def get_backfill_job(job_id: int):
    """Détail d'un job + tous ses items (pour la page de suivi)."""
    with get_session() as s:
        try:
            job = service.get_job_detail(s, job_id)
        except JobNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return BackfillJobDetailOut(
            **_serialize_job(job),
            items=[BackfillItemOut.model_validate(it) for it in job.items],
        )


@router.post("/jobs/{job_id}/pause", response_model=BackfillJobOut)
def pause_backfill_job(job_id: int):
    """Demande un pause coopératif. Le worker finit l'item en cours."""
    with get_session() as s:
        try:
            job = service.request_pause(s, job_id)
        except JobNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except BackfillError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return BackfillJobOut(**_serialize_job(job))


@router.post("/jobs/{job_id}/resume", response_model=BackfillJobOut)
def resume_backfill_job(job_id: int):
    """Relance un job en pause. Wake-up le worker."""
    with get_session() as s:
        try:
            job = service.resume_job(s, job_id)
        except JobNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except BackfillError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        serialized = _serialize_job(job)
    runner.ensure_running()
    return BackfillJobOut(**serialized)


@router.post("/jobs/{job_id}/cancel", response_model=BackfillJobOut)
def cancel_backfill_job(job_id: int):
    """Annule un job — marque les items pending comme skipped."""
    with get_session() as s:
        try:
            job = service.cancel_job(s, job_id)
        except JobNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except BackfillError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return BackfillJobOut(**_serialize_job(job))


# --- Helpers ----------------------------------------------------------------

def _serialize_job(job: BackfillJob) -> dict:
    """Sérialise les champs scalaires d'un job (pas les items, volontaire —
    évite l'eager-load involontaire sur les endpoints list)."""
    return {
        "id": job.id,
        "status": job.status,
        "source_type": job.source_type,
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "failed_items": job.failed_items,
        "inserted_quotes": job.inserted_quotes,
        "updated_quotes": job.updated_quotes,
        "pause_requested": job.pause_requested,
        "requested_by": job.requested_by,
        "message": job.message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "paused_at": job.paused_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
    }


# Suppress unused import warning for BackfillItem (used via relationship)
_ = BackfillItem
