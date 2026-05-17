"""Logique métier des jobs de backfill — create / pause / resume / status.

Séparé du router FastAPI (`api/backfill.py`) pour permettre l'usage depuis le
scheduler, CLI, ou tests sans charger FastAPI.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..models import BackfillItem, BackfillJob
from ..storage import StorageError, StorageNotConfigured, get_storage

logger = logging.getLogger(__name__)


# --- Errors -----------------------------------------------------------------

class BackfillError(RuntimeError):
    """Erreur métier (état invalide, transition refusée). 4xx côté HTTP."""


class JobNotFoundError(BackfillError):
    """Job introuvable → 404."""


# --- Ticker hint from filename ---------------------------------------------

# Pattern typique : "SNTS.csv", "SNTS_history.csv", "snts-2024.csv".
_TICKER_FROM_FILENAME = re.compile(
    r"^([A-Z]{2,8})(?:[_\-.\s]|$)", re.IGNORECASE,
)


def _guess_ticker_from_filename(filename: str) -> str | None:
    """Devine un ticker depuis un nom de fichier (case-insensitive).

    Retourne la version majuscule ou None si aucun pattern reconnu. Le runner
    valide ensuite contre la liste des tickers BRVM connus — si le ticker
    deviné n'est pas valide, on marque l'item comme failed.
    """
    if not filename:
        return None
    # Strip extension avant match
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    m = _TICKER_FROM_FILENAME.match(name)
    if not m:
        return None
    return m.group(1).upper()


def _sanitize_filename(filename: str) -> str:
    """Rend un nom de fichier safe pour une clé S3 (évite les chars spéciaux)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "unnamed")
    # Borne pour éviter les clés ridiculement longues
    return safe[:200] or "unnamed"


def _storage_key_for(job_id: int, item_id: int, filename: str) -> str:
    """Compose la clé S3 stable pour un item : `{job_id}/{item_id}/{filename}`."""
    return f"{job_id}/{item_id}/{_sanitize_filename(filename)}"


# --- Job lifecycle ----------------------------------------------------------

def create_job(
    session: Session,
    *,
    source_type: str,
    files: list[tuple[str, bytes]],
    requested_by: str | None = None,
) -> BackfillJob:
    """Crée un job + 1 item par fichier. Status initial : 'running'.

    Le runner est responsable de voir la nouvelle ligne via polling (ou on
    le démarre explicitement depuis l'API — cf `runner.py::ensure_running`).

    Args:
        source_type: "pdf_brvm" ou "csv".
        files: liste de (filename, blob) — blobs bruts issus du multipart.
        requested_by: email de l'utilisateur (trace).

    Raises:
        BackfillError: source_type invalide, fichiers vides, mix incohérent.
    """
    if source_type not in {"pdf_brvm", "csv"}:
        raise BackfillError(f"source_type invalide : {source_type!r}")
    if not files:
        raise BackfillError("Aucun fichier fourni.")

    kind = "pdf" if source_type == "pdf_brvm" else "csv"

    # Vérif précoce du storage — échoue avant de toucher la DB si mal configuré.
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise  # remonte tel quel — le router transforme en 503
    except Exception as e:  # noqa: BLE001
        raise BackfillError(f"Storage indisponible : {e}") from e

    job = BackfillJob(
        status="running",
        source_type=source_type,
        total_items=len(files),
        processed_items=0,
        failed_items=0,
        inserted_quotes=0,
        updated_quotes=0,
        pause_requested=False,
        requested_by=requested_by,
        started_at=datetime.now(UTC),
        message=f"Job créé avec {len(files)} fichier(s) — upload S3 en cours…",
    )
    session.add(job)
    session.flush()  # obtenir job.id pour FK

    # Crée d'abord les items (sans storage_key) pour avoir un item.id stable.
    # Puis upload vers S3 sous `{job_id}/{item_id}/{filename}` et renseigne la clé.
    # Si un upload échoue, l'item est marqué `failed` avec l'erreur.
    items_to_upload: list[tuple[BackfillItem, bytes]] = []
    for filename, blob in files:
        if not blob:
            session.add(BackfillItem(
                job_id=job.id,
                filename=filename,
                kind=kind,
                storage_key=None,
                status="failed",
                error="Fichier vide à l'upload.",
                processed_at=datetime.now(UTC),
            ))
            job.failed_items += 1
            continue
        ticker_hint = _guess_ticker_from_filename(filename) if kind == "csv" else None
        item = BackfillItem(
            job_id=job.id,
            filename=filename,
            kind=kind,
            storage_key=None,  # rempli juste après le put_object
            status="pending",
            ticker_hint=ticker_hint,
        )
        session.add(item)
        items_to_upload.append((item, blob))

    session.flush()  # alloue les item.id

    content_type = "application/pdf" if kind == "pdf" else "text/csv"
    for item, blob in items_to_upload:
        key = _storage_key_for(job.id, item.id, item.filename)
        try:
            storage.put_object(key, blob, content_type=content_type)
            item.storage_key = key
        except StorageError as e:
            # Upload raté → item failed, on continue avec les autres (partial job).
            logger.exception(f"[backfill] S3 put_object échec key={key!r}")
            item.status = "failed"
            item.error = f"Upload S3 échoué : {e}"
            item.processed_at = datetime.now(UTC)
            job.failed_items += 1

    session.flush()
    job.message = (
        f"Job créé avec {len(files)} fichier(s). "
        f"{job.failed_items} upload(s) en échec, "
        f"{job.total_items - job.failed_items} en attente de traitement."
    )
    logger.info(
        f"[backfill] Job #{job.id} créé : source={source_type} "
        f"items={len(files)} failed_upload={job.failed_items}",
    )
    return job


def request_pause(session: Session, job_id: int) -> BackfillJob:
    """Demande un pause coopératif. Le runner finira l'item en cours avant de
    passer le statut à 'paused'."""
    job = session.get(BackfillJob, job_id)
    if job is None:
        raise JobNotFoundError(f"Job #{job_id} introuvable.")
    if job.status not in {"running"}:
        raise BackfillError(
            f"Impossible de mettre en pause un job avec status={job.status!r}.",
        )
    job.pause_requested = True
    job.message = "Pause demandée — finalisation de l'item en cours…"
    logger.info(f"[backfill] Pause demandée sur job #{job_id}")
    return job


def resume_job(session: Session, job_id: int) -> BackfillJob:
    """Relance un job en pause ou annule un `pause_requested` en vol.

    Cas acceptés :
    - `status=paused` : transition classique après pause complétée ou orphelin.
    - `status=running AND pause_requested=True` : l'utilisateur a cliqué
      Pause puis Resume avant que le worker n'ait transitionné. On clear le
      flag → le worker continue normalement sans interruption visible.

    Les autres statuts terminaux (`completed`, `failed`, `cancelled`) sont
    refusés en 409.
    """
    job = session.get(BackfillJob, job_id)
    if job is None:
        raise JobNotFoundError(f"Job #{job_id} introuvable.")

    # Cas "pause annulée en vol" — trivial, on clear juste le flag.
    if job.status == "running" and job.pause_requested:
        job.pause_requested = False
        job.message = "Pause annulée — job continue normalement."
        logger.info(f"[backfill] Pause en vol annulée sur job #{job_id}")
        return job

    if job.status != "paused":
        raise BackfillError(
            f"Impossible de reprendre un job avec status={job.status!r} "
            f"(seuls 'paused' ou 'running+pause_requested' peuvent être relancés).",
        )

    # Vérif qu'il reste effectivement du travail
    remaining = session.execute(
        select(BackfillItem.id)
        .where(BackfillItem.job_id == job_id)
        .where(BackfillItem.status == "pending")
        .limit(1)
    ).scalar_one_or_none()
    if remaining is None:
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        job.message = "Aucun item en attente — job marqué comme complété."
        return job

    job.status = "running"
    job.pause_requested = False
    job.paused_at = None
    job.message = "Reprise demandée — worker en cours de redémarrage."
    logger.info(f"[backfill] Resume sur job #{job_id}")
    return job


def cancel_job(session: Session, job_id: int) -> BackfillJob:
    """Annule un job (items pending deviennent 'skipped', objets S3 supprimés).

    Les items déjà traités (done/failed) gardent leur état. Seuls les
    pending/processing sont skippés + leur blob S3 est libéré.
    """
    job = session.get(BackfillJob, job_id)
    if job is None:
        raise JobNotFoundError(f"Job #{job_id} introuvable.")
    if job.status in {"completed", "cancelled"}:
        raise BackfillError(
            f"Job déjà en état terminal : {job.status}",
        )

    # Collecte les storage_keys à supprimer AVANT l'update (sinon on les perd).
    keys_to_delete = [
        k for (k,) in session.execute(
            select(BackfillItem.storage_key)
            .where(BackfillItem.job_id == job_id)
            .where(BackfillItem.status.in_(["pending", "processing"]))
            .where(BackfillItem.storage_key.is_not(None))
        ).all()
    ]

    session.execute(
        update(BackfillItem)
        .where(BackfillItem.job_id == job_id)
        .where(BackfillItem.status.in_(["pending", "processing"]))
        .values(status="skipped", storage_key=None, error="Job annulé.")
    )
    job.status = "cancelled"
    job.pause_requested = False
    job.completed_at = datetime.now(UTC)
    job.message = "Job annulé — items pending marqués skipped, blobs S3 libérés."

    # Cleanup S3 best-effort — si ça échoue, on log mais on n'annule pas l'annulation.
    try:
        storage = get_storage()
        for k in keys_to_delete:
            try:
                storage.delete_object(k)
            except StorageError as e:
                logger.warning(f"[backfill] delete_object({k!r}) : {e}")
    except StorageNotConfigured:
        # Storage KO au moment de l'annulation — pas bloquant pour la DB.
        logger.warning("[backfill] Storage non configuré — cleanup S3 skippé.")

    logger.info(
        f"[backfill] Job #{job_id} annulé — {len(keys_to_delete)} objet(s) S3 libéré(s).",
    )
    return job


def reap_orphan_jobs(session: Session) -> int:
    """Au boot : tout job 'running' est orphelin (le worker est mort avec le
    process). On le passe à 'paused' pour permettre un resume explicite.

    Retourne le nombre de jobs reapés.
    """
    orphans = session.execute(
        select(BackfillJob).where(BackfillJob.status == "running")
    ).scalars().all()
    if not orphans:
        return 0
    now = datetime.now(UTC)
    for job in orphans:
        job.status = "paused"
        job.pause_requested = False
        job.paused_at = now
        job.message = (
            (job.message or "") +
            f"\n[{now.isoformat()}] Reap au boot — worker interrompu."
        ).strip()
    logger.info(f"[backfill] Reap de {len(orphans)} job(s) orphelin(s) au boot.")
    return len(orphans)


# --- Status queries ---------------------------------------------------------

def get_job_detail(session: Session, job_id: int) -> BackfillJob:
    """Récupère un job + eager-load ses items (pour l'UI)."""
    job = session.execute(
        select(BackfillJob)
        .options(selectinload(BackfillJob.items))
        .where(BackfillJob.id == job_id)
    ).scalar_one_or_none()
    if job is None:
        raise JobNotFoundError(f"Job #{job_id} introuvable.")
    return job


__all__ = [
    "BackfillError",
    "JobNotFoundError",
    "_guess_ticker_from_filename",
    "create_job",
    "request_pause",
    "resume_job",
    "cancel_job",
    "reap_orphan_jobs",
    "get_job_detail",
]
