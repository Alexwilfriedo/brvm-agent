"""Worker thread qui exécute les items `pending` d'un job de backfill.

Design :
  - **1 seul worker actif par process** (singleton). Si plusieurs requêtes
    `POST /jobs` arrivent, le worker traite les jobs en FIFO (par ordre de
    création) sans concurrence.
  - **Checkpoint par item** : chaque item est persisté en DB dès son
    traitement (done|failed). Un crash serveur n'en perd aucun.
  - **Pause coopérative** : le runner relit `job.pause_requested` entre
    chaque item. Il finit son item en cours avant de se stopper.
  - **Resume** : au `resume_job()`, le runner est wake-up via `ensure_running()`
    et reprend depuis les items encore `pending`.
  - **Advisory lock Postgres** : optionnel — à l'échelle mono-process Railway
    c'est le threading.Lock interne qui suffit. En multi-dyno ce serait
    nécessaire, mais ce projet ne scale pas horizontalement.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..collectors.historical_import import ImportCsvError, parse_historical_csv
from ..collectors.sika_quotes import BRVM_TICKERS
from ..database import get_session
from ..models import BackfillItem, BackfillJob, Quote
from ..storage import StorageError, get_storage
from .brvm_pdf import parse_brvm_pdf

logger = logging.getLogger(__name__)

# Singleton worker — 1 thread au max par process.
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_wake_event = threading.Event()
_shutdown_event = threading.Event()

# Référentiel ticker → (name, sector, country) pour enrichir les Quote créés.
_TICKER_META: dict[str, tuple[str, str, str]] = {
    t.ticker.upper(): (t.name, t.sector, t.country) for t in BRVM_TICKERS
}


def ensure_running() -> None:
    """Démarre le worker s'il n'est pas actif. Idempotent.

    Appelé :
      - Lifespan FastAPI au boot (au cas où un job reste `running`)
      - Après `create_job` (nouveau travail)
      - Après `resume_job` (reprise)
    """
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            # Déjà actif — on wake le thread au cas où il était en sleep.
            _wake_event.set()
            return
        _shutdown_event.clear()
        _wake_event.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, name="backfill-worker", daemon=True,
        )
        _worker_thread.start()
        logger.info("[backfill] Worker démarré.")


def shutdown_worker(timeout: float = 5.0) -> None:
    """Appelé par le lifespan au shutdown. Signal coopératif → join best-effort."""
    _shutdown_event.set()
    _wake_event.set()
    global _worker_thread
    t = _worker_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _worker_thread = None


# --- Main loop --------------------------------------------------------------

# Délai entre 2 polls quand il n'y a rien à faire. Dormir trop peu = CPU
# gaspillé ; dormir trop = latence visible au resume. 2s est un compromis.
_IDLE_POLL_SECONDS = 2.0


def _worker_loop() -> None:
    """Boucle principale — traite les jobs `running` un par un."""
    logger.info("[backfill] Boucle worker lancée.")
    while not _shutdown_event.is_set():
        try:
            job_id = _pick_next_running_job()
        except Exception:  # noqa: BLE001
            logger.exception("[backfill] Erreur pick_next_running_job")
            job_id = None

        if job_id is None:
            # Rien à faire — wait avec wake-up possible.
            _wake_event.wait(timeout=_IDLE_POLL_SECONDS)
            _wake_event.clear()
            continue

        try:
            _process_job(job_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"[backfill] Erreur fatale sur job #{job_id}")
            _fail_job(job_id, "Erreur interne — voir logs serveur.")


def _pick_next_running_job() -> int | None:
    """Retourne l'id du plus ancien job 'running' (FIFO)."""
    with get_session() as s:
        job = s.execute(
            select(BackfillJob)
            .where(BackfillJob.status == "running")
            .order_by(BackfillJob.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        return job.id if job else None


def _process_job(job_id: int) -> None:
    """Traite tous les items pending d'un job jusqu'à pause, complétion ou
    cancel. Rafraîchit l'état DB à chaque boundary pour observer les
    changements externes (pause, cancel)."""
    logger.info(f"[backfill] Start processing job #{job_id}")
    while not _shutdown_event.is_set():
        # 1. Fetch un item pending + check l'état courant du job
        with get_session() as s:
            job = s.get(BackfillJob, job_id)
            if job is None:
                logger.warning(f"[backfill] Job #{job_id} disparu en cours de traitement.")
                return
            if job.status != "running":
                logger.info(
                    f"[backfill] Job #{job_id} status={job.status} → worker sort de la boucle.",
                )
                return
            if job.pause_requested:
                _transition_to_paused(s, job)
                return

            item = s.execute(
                select(BackfillItem)
                .where(BackfillItem.job_id == job_id)
                .where(BackfillItem.status == "pending")
                .order_by(BackfillItem.id.asc())
                .limit(1)
            ).scalar_one_or_none()

            if item is None:
                _transition_to_completed(s, job)
                return

            # Marque processing pour éviter qu'un autre cycle ne le reprenne
            # (défense contre un hypothétique 2e worker).
            item.status = "processing"
            item_id = item.id
            kind = item.kind
            storage_key = item.storage_key
            filename = item.filename
            ticker_hint = item.ticker_hint

        # 2. Télécharge le blob depuis S3 puis traite (hors transaction DB).
        blob: bytes = b""
        download_error: str | None = None
        if storage_key:
            try:
                storage = get_storage()
                blob = storage.get_object(storage_key)
            except StorageError as e:
                logger.exception(f"[backfill] Item #{item_id} get_object échec")
                download_error = f"Download S3 échoué : {e}"
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[backfill] Item #{item_id} storage indisponible")
                download_error = f"Storage indisponible : {e}"
        else:
            download_error = "storage_key manquant (upload échoué initialement ?)"

        if download_error:
            inserted, updated, meta, error = 0, 0, {}, download_error
        else:
            try:
                if kind == "pdf":
                    inserted, updated, meta, error = _handle_pdf_item(filename, blob)
                elif kind == "csv":
                    inserted, updated, meta, error = _handle_csv_item(
                        filename, blob, ticker_hint=ticker_hint,
                    )
                else:
                    inserted, updated, meta, error = 0, 0, {}, f"Kind inconnu : {kind!r}"
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[backfill] Item #{item_id} exception")
                inserted, updated, meta, error = 0, 0, {}, f"exception: {e}"

        # 3. Checkpoint — persistence du résultat + cleanup S3 si succès.
        with get_session() as s:
            it = s.get(BackfillItem, item_id)
            if it is None:
                continue  # item supprimé entretemps
            if error:
                it.status = "failed"
                it.error = error[:2000]
                # Conserve storage_key pour permettre un retry futur.
            else:
                it.status = "done"
                # Succès → libère le blob S3 et oublie la clé.
                if it.storage_key:
                    try:
                        get_storage().delete_object(it.storage_key)
                    except Exception as cleanup_err:  # noqa: BLE001
                        logger.warning(
                            f"[backfill] delete_object({it.storage_key!r}) : {cleanup_err}"
                        )
                    it.storage_key = None
            it.inserted_quotes = inserted
            it.updated_quotes = updated
            it.meta = meta or {}
            it.processed_at = datetime.now(UTC)

            job = s.get(BackfillJob, job_id)
            if job is not None:
                job.processed_items += 1
                if error:
                    job.failed_items += 1
                job.inserted_quotes += inserted
                job.updated_quotes += updated
                job.message = (
                    f"{job.processed_items}/{job.total_items} traités "
                    f"({job.inserted_quotes} insert, {job.updated_quotes} update, "
                    f"{job.failed_items} erreurs)."
                )


def _transition_to_paused(s, job: BackfillJob) -> None:
    """Job → paused proprement."""
    job.status = "paused"
    job.pause_requested = False
    job.paused_at = datetime.now(UTC)
    job.message = (
        f"Pausé après {job.processed_items}/{job.total_items} items "
        f"({job.inserted_quotes} quotes insérées)."
    )
    logger.info(f"[backfill] Job #{job.id} passé en pause.")


def _transition_to_completed(s, job: BackfillJob) -> None:
    """Job → completed (plus aucun item pending)."""
    job.status = "completed"
    job.pause_requested = False
    job.completed_at = datetime.now(UTC)
    job.message = (
        f"Terminé : {job.processed_items}/{job.total_items} items, "
        f"{job.inserted_quotes} insertions, {job.updated_quotes} mises à jour, "
        f"{job.failed_items} erreurs."
    )
    logger.info(f"[backfill] Job #{job.id} complété.")


def _fail_job(job_id: int, message: str) -> None:
    """Job → failed (erreur fatale non récupérable)."""
    with get_session() as s:
        job = s.get(BackfillJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.pause_requested = False
        job.completed_at = datetime.now(UTC)
        job.message = message
        logger.error(f"[backfill] Job #{job_id} failed : {message}")


# --- Item handlers ----------------------------------------------------------

def _handle_pdf_item(
    filename: str,
    blob: bytes,
) -> tuple[int, int, dict, str | None]:
    """Parse un PDF BRVM et upsert les quotes extraites.

    Retourne `(inserted, updated, meta, error)` — error est None en cas de succès.
    """
    result = parse_brvm_pdf(blob, filename=filename)
    meta: dict = {
        "parsed_from": result.parsed_from,
        "quotes_found": len(result.quotes),
        "parser_errors": result.errors[:10],
    }
    if result.quote_date is None:
        return 0, 0, meta, (
            "Date du bulletin non détectée (ni dans le texte ni dans le filename). "
            "Renomme le fichier avec la date, ex: 'boc_15_01_2024.pdf'."
        )
    if not result.quotes:
        return 0, 0, meta, (
            "Aucune cotation extraite du PDF. Détail : "
            + "; ".join(result.errors[:3])
        )

    meta["quote_date"] = result.quote_date.date().isoformat()
    inserted, updated = _upsert_quotes_from_pdf(
        quote_date=result.quote_date,
        quotes=result.quotes,
    )
    return inserted, updated, meta, None


def _handle_csv_item(
    filename: str,
    blob: bytes,
    *,
    ticker_hint: str | None,
) -> tuple[int, int, dict, str | None]:
    """Parse un CSV d'historique et upsert les quotes. Le ticker vient du hint
    (nom de fichier). Si le hint est absent ou pas dans le référentiel BRVM,
    on refuse l'item."""
    if not ticker_hint:
        return 0, 0, {}, (
            "Ticker non détecté dans le nom de fichier. Renomme "
            f"{filename!r} en commençant par le ticker, ex: 'SNTS_history.csv'."
        )
    ticker = ticker_hint.strip().upper()
    if ticker not in _TICKER_META:
        return 0, 0, {}, (
            f"Ticker {ticker!r} inconnu du référentiel BRVM. "
            f"Vérifie le nom de fichier."
        )

    try:
        parsed = parse_historical_csv(blob)
    except ImportCsvError as e:
        return 0, 0, {}, f"CSV invalide : {e}"

    meta = {
        "ticker": ticker,
        "rows_parsed": len(parsed.rows),
        "rows_skipped": parsed.skipped,
        "detected_delimiter": parsed.detected_delimiter,
        "parser_errors": parsed.errors[:10],
    }
    if not parsed.rows:
        return 0, 0, meta, "Aucune ligne exploitable dans le CSV."

    inserted, updated = _upsert_quotes_from_csv(ticker=ticker, rows=parsed.rows)
    return inserted, updated, meta, None


# --- Upsert helpers ---------------------------------------------------------

def _upsert_quotes_from_pdf(
    *,
    quote_date: datetime,
    quotes: list,
) -> tuple[int, int]:
    """Bulk upsert des cotations d'une séance (1 PDF = 1 date, N tickers)."""
    if not quotes:
        return 0, 0

    values = []
    for q in quotes:
        meta = _TICKER_META.get(q.ticker.upper())
        if meta is None:
            continue
        name, sector, country = meta
        values.append({
            "ticker": q.ticker.upper(),
            "name": name,
            "sector": sector,
            "country": country,
            "quote_date": quote_date,
            "close_price": q.close_price,
            "variation_pct": q.variation_pct or 0.0,
            "volume": q.volume,
            "value_traded": q.value_traded,
            "extras": {},
        })

    if not values:
        return 0, 0

    return _bulk_upsert(values)


def _upsert_quotes_from_csv(
    *,
    ticker: str,
    rows: list,
) -> tuple[int, int]:
    """Bulk upsert des cotations historiques d'un ticker (1 CSV)."""
    if not rows:
        return 0, 0

    name, sector, country = _TICKER_META[ticker]
    values = []
    for r in rows:
        extras: dict = {}
        if r.open_price is not None:
            extras["open_price"] = r.open_price
        if r.high_price is not None:
            extras["high_price"] = r.high_price
        if r.low_price is not None:
            extras["low_price"] = r.low_price
        values.append({
            "ticker": ticker,
            "name": name,
            "sector": sector,
            "country": country,
            "quote_date": r.quote_date,
            "close_price": r.close_price,
            "variation_pct": r.variation_pct if r.variation_pct is not None else 0.0,
            "volume": r.volume,
            "value_traded": r.value_traded,
            "extras": extras,
        })

    return _bulk_upsert(values)


def _bulk_upsert(values: list[dict]) -> tuple[int, int]:
    """Upsert Postgres ON CONFLICT (ticker, quote_date).

    Retourne (inserted, updated). On compte en interrogeant les dates
    existantes avant l'insert — précis mais coûte 1 SELECT supplémentaire.
    Alternative (`xmax = 0`) non portable → on reste simple.
    """
    if not values:
        return 0, 0

    tickers = {v["ticker"] for v in values}
    dates = {v["quote_date"] for v in values}

    with get_session() as s:
        existing = s.execute(
            select(Quote.ticker, Quote.quote_date)
            .where(Quote.ticker.in_(tickers))
            .where(Quote.quote_date.in_(dates))
        ).all()
        existing_set = {(t, d) for t, d in existing}

        inserted = 0
        updated = 0
        for v in values:
            if (v["ticker"], v["quote_date"]) in existing_set:
                updated += 1
            else:
                inserted += 1

        stmt = pg_insert(Quote).values(values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker", "quote_date"],
            set_={
                "close_price": excluded.close_price,
                "variation_pct": excluded.variation_pct,
                "volume": excluded.volume,
                "value_traded": excluded.value_traded,
                # Merge JSON : préserve les clés existantes (PER/RSI du cron)
                # et ajoute/écrase celles du backfill.
                "extras": Quote.__table__.c.extras.op("||")(excluded.extras),
            },
        )
        s.execute(stmt)

    return inserted, updated


__all__ = ["ensure_running", "shutdown_worker"]
