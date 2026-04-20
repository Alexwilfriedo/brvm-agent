"""Endpoints admin pour la config du scheduler."""
from datetime import UTC, datetime

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from ..database import get_session
from ..models import ScheduleConfig
from ..scheduler import get_scheduler
from .deps import require_admin

router = APIRouter(prefix="/api/schedule", tags=["schedule"], dependencies=[Depends(require_admin)])


class ScheduleOut(BaseModel):
    cron_expression: str
    enabled: bool
    updated_at: datetime
    next_run: str | None = None


class SchedulePatch(BaseModel):
    cron_expression: str | None = None
    enabled: bool | None = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v):
        if v is None:
            return v
        try:
            CronTrigger.from_crontab(v)
        except Exception as e:
            raise ValueError(f"Expression cron invalide : {e}") from e
        return v


@router.get("", response_model=ScheduleOut)
def get_schedule():
    scheduler = get_scheduler()
    with get_session() as s:
        cfg = s.execute(select(ScheduleConfig).limit(1)).scalar_one_or_none()
        if not cfg:
            raise HTTPException(status_code=404, detail="Config inexistante")
        job = scheduler.scheduler.get_job("daily_brief")
        next_run = str(job.next_run_time) if job else None
        return ScheduleOut(
            cron_expression=cfg.cron_expression,
            enabled=cfg.enabled,
            updated_at=cfg.updated_at,
            next_run=next_run,
        )


@router.patch("", response_model=ScheduleOut)
def update_schedule(body: SchedulePatch):
    with get_session() as s:
        cfg = s.execute(select(ScheduleConfig).limit(1)).scalar_one_or_none()
        if not cfg:
            raise HTTPException(status_code=404, detail="Config inexistante")
        if body.cron_expression is not None:
            cfg.cron_expression = body.cron_expression
        if body.enabled is not None:
            cfg.enabled = body.enabled
        cfg.updated_at = datetime.now(UTC)
        s.flush()

    # Hot-reload du scheduler
    get_scheduler().reload()
    return get_schedule()


@router.post("/run-now", status_code=202)
def run_now():
    """Déclenche une exécution immédiate. Ne bloque pas."""
    get_scheduler().trigger_now()
    return {"status": "scheduled", "message": "Pipeline déclenché en arrière-plan"}
