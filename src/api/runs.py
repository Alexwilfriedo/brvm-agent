"""Endpoints pour consulter l'historique des exécutions du pipeline."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..database import get_session
from ..models import PipelineRun
from .deps import require_admin

router = APIRouter(prefix="/api/runs", tags=["runs"], dependencies=[Depends(require_admin)])


class RunOut(BaseModel):
    id: int
    started_at: datetime
    ended_at: datetime | None
    status: str
    trigger: str
    brief_id: int | None
    error: str | None
    summary: dict

    class Config:
        from_attributes = True


@router.get("", response_model=list[RunOut])
def list_runs(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = Query(None, description="Filter by status"),
):
    with get_session() as s:
        stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(PipelineRun.status == status)
        runs = s.execute(stmt).scalars().all()
        return [RunOut.model_validate(r) for r in runs]


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int):
    with get_session() as s:
        run = s.get(PipelineRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run introuvable")
        return RunOut.model_validate(run)
