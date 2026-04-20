"""Endpoints admin pour la config du scheduler."""
from datetime import UTC, datetime

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Query
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
    scheduler_running: bool = False


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
        # "Running" = APScheduler actif ET un job daily_brief est enregistré.
        # Sans le job, le cron config peut exister mais rien ne se déclencherait.
        is_running = bool(scheduler.scheduler.running and job)
        return ScheduleOut(
            cron_expression=cfg.cron_expression,
            enabled=cfg.enabled,
            updated_at=cfg.updated_at,
            next_run=next_run,
            scheduler_running=is_running,
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
def run_now(
    force: bool = Query(
        False,
        description=(
            "True pour régénérer un brief déjà existant aujourd'hui "
            "(créera une révision 2+). Par défaut, le run skippe si un brief "
            "du jour existe déjà."
        ),
    ),
):
    """Déclenche une exécution immédiate. Ne bloque pas.

    Comportement par défaut (force=False) : idempotent — si un brief existe
    déjà pour la date du jour, le run skippe avec `status=already_generated`.
    Passer `?force=true` pour forcer la régénération (= révision).
    """
    get_scheduler().trigger_now(force=force)
    return {
        "status": "scheduled",
        "force": force,
        "message": (
            "Régénération forcée en arrière-plan" if force
            else "Pipeline déclenché (idempotent — skip si brief du jour existe)"
        ),
    }
