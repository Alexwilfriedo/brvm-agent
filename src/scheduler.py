"""Wrapper APScheduler : lit le cron depuis la DB, supporte le reload à chaud.

Le reload permet à l'API admin de changer le cron sans redéployer :
il suffit d'appeler `scheduler_manager.reload()` après avoir modifié
`ScheduleConfig` en DB.
"""
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import get_settings
from .database import get_session
from .models import ScheduleConfig
from .pipeline import run_daily_pipeline

logger = logging.getLogger(__name__)

JOB_ID = "daily_brief"
MANUAL_JOB_ID = "daily_brief_manual"


def _run_scheduled() -> None:
    """Wrapper cron — injecte trigger='cron', idempotent par date."""
    run_daily_pipeline(trigger="cron", force=False)


def _run_manual_idempotent() -> None:
    """Wrapper API, pas de régénération si brief du jour existe déjà."""
    run_daily_pipeline(trigger="manual", force=False)


def _run_manual_force() -> None:
    """Wrapper API avec régénération explicite (→ revision 2+)."""
    run_daily_pipeline(trigger="manual", force=True)


class SchedulerManager:
    def __init__(self):
        self.settings = get_settings()
        self.tz = ZoneInfo(self.settings.timezone)
        self.scheduler = BackgroundScheduler(timezone=self.tz)

    def start(self) -> None:
        self.scheduler.start()
        self.reload()
        logger.info("Scheduler démarré")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reload(self) -> dict:
        """Recharge le cron depuis la DB. Appelé au startup et après toute modification via API."""
        cron_expr, enabled = self._get_config_from_db()

        if self.scheduler.get_job(JOB_ID):
            self.scheduler.remove_job(JOB_ID)

        if not enabled:
            logger.info("Scheduler désactivé en config")
            return {"status": "disabled", "cron": cron_expr}

        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone=self.tz)
        except ValueError as e:
            logger.error(f"Cron invalide '{cron_expr}': {e}. Fallback sur default.")
            cron_expr = self.settings.default_cron
            trigger = CronTrigger.from_crontab(cron_expr, timezone=self.tz)

        self.scheduler.add_job(
            _run_scheduled,
            trigger=trigger,
            id=JOB_ID,
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
        )
        next_run = self.scheduler.get_job(JOB_ID).next_run_time
        logger.info(f"Job planifié : cron='{cron_expr}', prochain run = {next_run}")
        return {"status": "enabled", "cron": cron_expr, "next_run": str(next_run)}

    def trigger_now(self, force: bool = False) -> None:
        """Déclenche une exécution immédiate en arrière-plan.

        Args:
            force: True pour régénérer le brief du jour même s'il existe
                   déjà (crée une révision). False (défaut) = idempotent.
        """
        self.scheduler.add_job(
            _run_manual_force if force else _run_manual_idempotent,
            id=MANUAL_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"Déclenchement manuel programmé (force={force})")

    def _get_config_from_db(self) -> tuple[str, bool]:
        with get_session() as s:
            cfg = s.execute(select(ScheduleConfig).limit(1)).scalar_one_or_none()
            if cfg:
                return cfg.cron_expression, cfg.enabled
            cfg = ScheduleConfig(cron_expression=self.settings.default_cron, enabled=True)
            s.add(cfg)
        return self.settings.default_cron, True


scheduler_manager: SchedulerManager | None = None


def get_scheduler() -> SchedulerManager:
    global scheduler_manager
    if scheduler_manager is None:
        scheduler_manager = SchedulerManager()
    return scheduler_manager
