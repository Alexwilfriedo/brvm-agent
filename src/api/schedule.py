"""Endpoints admin pour la config du scheduler (daily + weekly)."""
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
    weekly_cron_expression: str | None = None
    enabled: bool
    updated_at: datetime
    next_run: str | None = None
    weekly_next_run: str | None = None
    scheduler_running: bool = False


class SchedulePatch(BaseModel):
    cron_expression: str | None = None
    # Envoyer `null` vide explicitement le weekly (=désactive). Omettre le
    # champ = ne touche pas à la valeur actuelle. Pour distinguer les deux
    # côté wire on lit le body brut via `model_fields_set`.
    weekly_cron_expression: str | None = None
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

    @field_validator("weekly_cron_expression")
    @classmethod
    def validate_weekly_cron(cls, v):
        if v is None or v == "":
            return None  # null / "" → désactive le weekly
        try:
            CronTrigger.from_crontab(v)
        except Exception as e:
            raise ValueError(f"Expression cron weekly invalide : {e}") from e
        return v


@router.get("", response_model=ScheduleOut)
def get_schedule():
    scheduler = get_scheduler()
    with get_session() as s:
        cfg = s.execute(select(ScheduleConfig).limit(1)).scalar_one_or_none()
        if not cfg:
            raise HTTPException(status_code=404, detail="Config inexistante")
        daily_job = scheduler.scheduler.get_job("daily_brief")
        weekly_job = scheduler.scheduler.get_job("weekly_brief")
        # next_run_time n'est peuplé que si le scheduler tourne. En dev/tests
        # il peut être absent même si le job existe.
        _dn = getattr(daily_job, "next_run_time", None) if daily_job else None
        _wn = getattr(weekly_job, "next_run_time", None) if weekly_job else None
        next_run = str(_dn) if _dn else None
        weekly_next_run = str(_wn) if _wn else None
        # "Running" = APScheduler actif ET le job daily est enregistré.
        is_running = bool(scheduler.scheduler.running and daily_job)
        return ScheduleOut(
            cron_expression=cfg.cron_expression,
            weekly_cron_expression=cfg.weekly_cron_expression,
            enabled=cfg.enabled,
            updated_at=cfg.updated_at,
            next_run=next_run,
            weekly_next_run=weekly_next_run,
            scheduler_running=is_running,
        )


@router.patch("", response_model=ScheduleOut)
def update_schedule(body: SchedulePatch):
    with get_session() as s:
        cfg = s.execute(select(ScheduleConfig).limit(1)).scalar_one_or_none()
        if not cfg:
            raise HTTPException(status_code=404, detail="Config inexistante")
        fields_sent = body.model_fields_set
        if "cron_expression" in fields_sent and body.cron_expression is not None:
            cfg.cron_expression = body.cron_expression
        # weekly_cron_expression : explicitement présent dans le body → mise à jour
        # (peut être passé à NULL pour désactiver). Sinon : on ne touche pas.
        if "weekly_cron_expression" in fields_sent:
            cfg.weekly_cron_expression = body.weekly_cron_expression
        if "enabled" in fields_sent and body.enabled is not None:
            cfg.enabled = body.enabled
        cfg.updated_at = datetime.now(UTC)
        s.flush()

    # Hot-reload du scheduler (recalcule les deux jobs)
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
    """Déclenche une exécution immédiate du pipeline **daily**. Ne bloque pas.

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


@router.post("/run-weekly-now", status_code=202)
def run_weekly_now(
    force: bool = Query(
        False,
        description=(
            "True pour régénérer le brief hebdo même s'il existe déjà pour "
            "la semaine courante (créera une révision 2+)."
        ),
    ),
):
    """Déclenche une exécution immédiate du pipeline **weekly**. Ne bloque pas.

    La fenêtre est calculée automatiquement : dernière semaine de trading
    terminée (lundi → vendredi le plus récent). Utile pour tester sans attendre
    samedi matin.

    Si `weekly_cron_expression` est NULL en DB, ça marche quand même — la
    présence du cron ne conditionne que le cron automatique.
    """
    get_scheduler().trigger_weekly_now(force=force)
    return {
        "status": "scheduled",
        "force": force,
        "message": (
            "Brief hebdo : régénération forcée en arrière-plan" if force
            else "Brief hebdo : déclenché (idempotent — skip si weekly existe)"
        ),
    }
