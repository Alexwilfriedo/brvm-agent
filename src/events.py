"""Event bus in-memory pour streamer les événements d'un run en direct (SSE).

Design :
- Un run_id ↔ 0..N abonnés (clients SSE). Chaque abonné a sa propre `Queue`.
- `publish(run_id, event, payload)` envoie l'event à tous les abonnés.
- Les events sont aussi **stockés en RAM** dans un buffer circulaire par run_id
  (100 derniers) pour que les abonnés qui se connectent en cours de run
  reçoivent immédiatement l'historique depuis le début de la session.
- Plus de scope nécessaire pour un déploiement mono-replica (Railway 1 dyno).
  Pour scaler, passer à Postgres LISTEN/NOTIFY ou Redis pub/sub.

Thread-safe via `threading.Lock` — utilisable depuis des threads worker
(ThreadPoolExecutor du collector) **et** depuis le thread FastAPI.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# Buffer des derniers events par run_id (pour rejouer à un nouveau client SSE)
_HISTORY_SIZE = 500
_history: dict[int, deque] = {}

# Subscribers actifs : run_id → liste de Queue[Event]
_subscribers: dict[int, list[queue.Queue]] = {}

# Timers de purge trackés pour pouvoir les annuler au shutdown — sinon on
# fuit un thread par run pendant 5 minutes.
_purge_timers: dict[int, threading.Timer] = {}

_lock = threading.Lock()


def publish(run_id: int, event: str, **payload: Any) -> None:
    """Publie un event à tous les abonnés d'un run_id."""
    evt = {
        "t": time.time(),
        "event": event,
        "run_id": run_id,
        **payload,
    }
    with _lock:
        buf = _history.setdefault(run_id, deque(maxlen=_HISTORY_SIZE))
        buf.append(evt)
        subs = list(_subscribers.get(run_id, []))
    # Push sans bloquer si subscriber lent
    for q in subs:
        try:
            q.put_nowait(evt)
        except queue.Full:
            logger.warning(f"Subscriber queue plein pour run {run_id}, event drop")


def subscribe(run_id: int) -> tuple[queue.Queue, list[dict]]:
    """Inscrit un nouveau subscriber. Retourne (queue, historique depuis début).

    L'appelant doit appeler `unsubscribe(run_id, queue)` quand il ferme
    la connexion (dans un `finally` côté générateur SSE).
    """
    q: queue.Queue = queue.Queue(maxsize=1000)
    with _lock:
        buf = _history.get(run_id, deque())
        history = list(buf)
        _subscribers.setdefault(run_id, []).append(q)
    return q, history


def unsubscribe(run_id: int, q: queue.Queue) -> None:
    with _lock:
        subs = _subscribers.get(run_id)
        if subs and q in subs:
            subs.remove(q)
        if subs == []:
            _subscribers.pop(run_id, None)


def _purge_history(run_id: int) -> None:
    """Purge atomique d'un run de l'historique + nettoie le tracker de timer."""
    with _lock:
        _history.pop(run_id, None)
        _purge_timers.pop(run_id, None)


def mark_run_done(run_id: int) -> None:
    """Envoie un event `run.closed` + programme la purge de l'historique.

    Le buffer est conservé 5 min pour permettre à un client qui se reconnecte
    de rejouer la fin du run. Le timer est **tracké** pour pouvoir l'annuler
    au shutdown (sinon fuite d'un thread par run pendant 5 minutes).
    """
    publish(run_id, "run.closed")
    timer = threading.Timer(300, _purge_history, args=(run_id,))
    timer.daemon = True  # le thread n'empêche pas l'arrêt de l'app
    with _lock:
        # Si un timer existait déjà (double mark_run_done), on l'annule
        previous = _purge_timers.pop(run_id, None)
        if previous:
            previous.cancel()
        _purge_timers[run_id] = timer
    timer.start()


def shutdown() -> None:
    """Annule tous les timers de purge en cours (appelé depuis le lifespan).

    Sans ça, un SIGTERM pendant les 5 min post-run laisse le thread vivre
    jusqu'à son échéance même si l'app s'arrête.
    """
    with _lock:
        for timer in _purge_timers.values():
            timer.cancel()
        _purge_timers.clear()
