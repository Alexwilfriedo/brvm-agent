"""Wrapper APScheduler : lit les crons depuis la DB, supporte le reload à chaud.

Deux jobs distincts :
- `daily_brief` : cron obligatoire (default `0 8 * * *`), pilote `run_daily_pipeline`
- `weekly_brief` : cron optionnel (default `0 7 * * 6` = samedi 7h), pilote
  `run_weekly_pipeline`. Si `weekly_cron_expression` est NULL en DB, le job
  weekly n'est pas enregistré — pas d'envoi de brief hebdo.

Le reload permet à l'API admin de changer les crons sans redéployer :
il suffit d'appeler `scheduler_manager.reload()` après avoir modifié
`ScheduleConfig` en DB.
"""
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import get_settings
from .database import get_session
from .models import ScheduleConfig
from .pipeline import run_daily_pipeline, run_weekly_pipeline

logger = logging.getLogger(__name__)

# Daily
JOB_ID = "daily_brief"
MANUAL_JOB_ID = "daily_brief_manual"
# Weekly
WEEKLY_JOB_ID = "weekly_brief"
WEEKLY_MANUAL_JOB_ID = "weekly_brief_manual"


@dataclass
class ScheduleSnapshot:
    """Représentation lue de la config planning (single row)."""
    cron_expression: str
    weekly_cron_expression: str | None
    enabled: bool


def _run_scheduled() -> None:
    """Wrapper cron daily — idempotent par date."""
    run_daily_pipeline(trigger="cron", force=False)


def _run_manual_idempotent() -> None:
    """Wrapper API, pas de régénération si brief du jour existe déjà."""
    run_daily_pipeline(trigger="manual", force=False)


def _run_manual_force() -> None:
    """Wrapper API avec régénération explicite (→ revision 2+)."""
    run_daily_pipeline(trigger="manual", force=True)


def _run_weekly_scheduled() -> None:
    """Wrapper cron weekly — idempotent par semaine (vendredi de clôture)."""
    run_weekly_pipeline(trigger="cron", force=False)


def _run_weekly_manual_idempotent() -> None:
    run_weekly_pipeline(trigger="manual", force=False)


def _run_weekly_manual_force() -> None:
    run_weekly_pipeline(trigger="manual", force=True)


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
        """Recharge les crons depuis la DB. Appelé au startup et après toute
        modification via API. Gère daily ET weekly indépendamment."""
        snapshot = self._get_config_from_db()

        # Toujours retirer les jobs existants avant de décider (évite que
        # l'ancien cron reste actif si la config passe de "enabled" à "disabled").
        for jid in (JOB_ID, WEEKLY_JOB_ID):
            if self.scheduler.get_job(jid):
                self.scheduler.remove_job(jid)

        if not snapshot.enabled:
            logger.info("Scheduler désactivé en config (master switch)")
            return {
                "status": "disabled",
                "daily_cron": snapshot.cron_expression,
                "weekly_cron": snapshot.weekly_cron_expression,
            }

        # --- Daily (obligatoire) ---
        daily_cron = snapshot.cron_expression
        try:
            daily_trigger = CronTrigger.from_crontab(daily_cron, timezone=self.tz)
        except ValueError as e:
            logger.error(
                f"Cron daily invalide '{daily_cron}': {e}. Fallback sur default."
            )
            daily_cron = self.settings.default_cron
            daily_trigger = CronTrigger.from_crontab(daily_cron, timezone=self.tz)

        self.scheduler.add_job(
            _run_scheduled, trigger=daily_trigger,
            id=JOB_ID, replace_existing=True,
            misfire_grace_time=600, coalesce=True,
        )
        daily_job = self.scheduler.get_job(JOB_ID)
        # next_run_time n'est peuplé qu'à partir du moment où le scheduler tourne.
        # En tests / avant start(), le job existe mais l'attribut peut être absent.
        daily_next = getattr(daily_job, "next_run_time", None)

        # --- Weekly (optionnel) ---
        weekly_cron = snapshot.weekly_cron_expression
        weekly_next = None
        if weekly_cron:
            try:
                weekly_trigger = CronTrigger.from_crontab(weekly_cron, timezone=self.tz)
                self.scheduler.add_job(
                    _run_weekly_scheduled, trigger=weekly_trigger,
                    id=WEEKLY_JOB_ID, replace_existing=True,
                    # Grace plus large : si restart samedi matin, on veut
                    # quand même que le weekly parte.
                    misfire_grace_time=3600, coalesce=True,
                )
                weekly_job = self.scheduler.get_job(WEEKLY_JOB_ID)
                weekly_next = getattr(weekly_job, "next_run_time", None)
            except ValueError as e:
                logger.error(
                    f"Cron weekly invalide '{weekly_cron}': {e}. Weekly non planifié."
                )
                weekly_cron = None

        logger.info(
            f"Jobs planifiés — daily='{daily_cron}' (next={daily_next}), "
            f"weekly={weekly_cron or '<désactivé>'} (next={weekly_next})"
        )
        return {
            "status": "enabled",
            "daily_cron": daily_cron,
            "daily_next_run": str(daily_next),
            "weekly_cron": weekly_cron,
            "weekly_next_run": str(weekly_next) if weekly_next else None,
        }

    def trigger_now(self, force: bool = False) -> None:
        """Déclenche le pipeline daily immédiatement en arrière-plan."""
        self.scheduler.add_job(
            _run_manual_force if force else _run_manual_idempotent,
            id=MANUAL_JOB_ID, replace_existing=True,
        )
        logger.info(f"Déclenchement manuel daily programmé (force={force})")

    def trigger_weekly_now(self, force: bool = False) -> None:
        """Déclenche le pipeline weekly immédiatement en arrière-plan.

        Utile pour que l'admin puisse tester sans attendre samedi. `force=True`
        regénère même si un brief hebdo existe déjà pour la semaine courante
        (→ révision 2+).
        """
        self.scheduler.add_job(
            _run_weekly_manual_force if force else _run_weekly_manual_idempotent,
            id=WEEKLY_MANUAL_JOB_ID, replace_existing=True,
        )
        logger.info(f"Déclenchement manuel weekly programmé (force={force})")

    def _get_config_from_db(self) -> ScheduleSnapshot:
        with get_session() as s:
            cfg = s.execute(select(ScheduleConfig).limit(1)).scalar_one_or_none()
            if cfg:
                return ScheduleSnapshot(
                    cron_expression=cfg.cron_expression,
                    weekly_cron_expression=cfg.weekly_cron_expression,
                    enabled=cfg.enabled,
                )
            # Seed initial : crée la config avec le default daily, mais laisse
            # le weekly à NULL — l'admin doit l'activer explicitement via l'UI
            # pour éviter d'envoyer des briefs hebdo non voulus sur un nouveau déploiement.
            cfg = ScheduleConfig(
                cron_expression=self.settings.default_cron,
                weekly_cron_expression=None,
                enabled=True,
            )
            s.add(cfg)
        return ScheduleSnapshot(
            cron_expression=self.settings.default_cron,
            weekly_cron_expression=None,
            enabled=True,
        )


scheduler_manager: SchedulerManager | None = None


def get_scheduler() -> SchedulerManager:
    global scheduler_manager
    if scheduler_manager is None:
        scheduler_manager = SchedulerManager()
    return scheduler_manager
