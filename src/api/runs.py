"""Endpoints pour consulter l'historique des exécutions du pipeline."""
import asyncio
import json
import queue as queue_mod
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import String, cast, select

from .. import events
from ..auth.tokens import InvalidSessionError, decode_session_jwt
from ..config import get_settings
from ..database import get_session
from ..models import NewsArticle, PipelineRun, Source, User
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, PaginatedResponse, ilike_any, paginate

# Router principal — toutes les routes passent par require_admin.
router = APIRouter(prefix="/api/runs", tags=["runs"], dependencies=[Depends(require_admin)])

# Router séparé pour le SSE : pas de dependency globale, l'auth est faite à la
# main dans le handler parce qu'EventSource ne peut pas envoyer de headers
# custom (on accepte le token en query-string).
stream_router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    ended_at: datetime | None
    status: str
    trigger: str
    brief_id: int | None
    error: str | None
    summary: dict


@router.get("", response_model=PaginatedResponse[RunOut])
def list_runs(
    q: str | None = Query(None, description="Recherche fuzzy dans status/trigger/error"),
    status: str | None = Query(None, description="Filtre strict sur status"),
    trigger: str | None = Query(None, description="Filtre strict sur trigger"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with get_session() as s:
        stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc())
        if status:
            stmt = stmt.where(PipelineRun.status == status)
        if trigger:
            stmt = stmt.where(PipelineRun.trigger == trigger)
        if q:
            stmt = stmt.where(
                ilike_any([
                    cast(PipelineRun.status, String),
                    cast(PipelineRun.trigger, String),
                    PipelineRun.error,
                ], q)
            )
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[RunOut](
            items=[RunOut.model_validate(r) for r in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int):
    with get_session() as s:
        run = s.get(PipelineRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run introuvable")
        return RunOut.model_validate(run)


# --- Recap par source -----------------------------------------------------

class RunNewsOut(BaseModel):
    """Aperçu d'un article capturé dans un run — version compacte pour la UI."""
    id: int | None
    source_key: str
    title: str
    url: str
    published_at: datetime | None
    tickers_mentioned: list[str]
    enriched: bool


class RunSourceOut(BaseModel):
    """Recap d'une source pour un run donné.

    `news_urls` : toutes les news vues lors du collect (y compris doublons déjà
                  en DB). Borné à 100 dans le pipeline pour limiter la taille
                  de PipelineRun.summary.
    `new_news_urls` : uniquement les news nouvellement insérées lors de ce run
                      (celles qui sont parties en enrichment Sonnet).
    `news` : l'hydratation côté table `news` — titre, tickers, status enrichment.
    """
    source_key: str
    source_name: str | None
    source_type: str | None
    news_count: int
    quotes_count: int
    new_news_count: int
    errors: list[str]
    news: list[RunNewsOut]


@router.get("/{run_id}/sources", response_model=list[RunSourceOut])
def get_run_sources(run_id: int, limit_news_per_source: int = Query(20, ge=1, le=100)):
    """Recap détaillé par source pour un run donné.

    Répond au besoin "voir ce que chaque source a ramené dans ce run" sans
    avoir à regarder les events SSE (purgés après 5 min) ni à farfouiller en DB.
    """
    with get_session() as s:
        run = s.get(PipelineRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run introuvable")

        by_source = (run.summary or {}).get("by_source") or []
        if not by_source:
            # Backward compat : runs antérieurs au déploiement de cette fonction.
            # On retourne une vue dégradée basée sur le step collect s'il existe,
            # sinon rien.
            for step in (run.summary or {}).get("steps", []):
                if step.get("step") == "collect" and step.get("by_source"):
                    by_source = step["by_source"]
                    break

        # Map source_key → (name, type) pour enrichir l'affichage UI
        source_meta: dict[str, Source] = {
            src.key: src
            for src in s.execute(select(Source)).scalars().all()
        }

        # On charge toutes les news concernées par URL en 1 requête (évite
        # N requêtes — lefaso peut avoir 50+ URLs).
        all_urls: list[str] = []
        for entry in by_source:
            all_urls.extend(entry.get("news_urls") or [])
        news_by_url: dict[str, NewsArticle] = {}
        if all_urls:
            rows = s.execute(
                select(NewsArticle).where(NewsArticle.url.in_(all_urls))
            ).scalars().all()
            news_by_url = {n.url: n for n in rows}

        out: list[RunSourceOut] = []
        for entry in by_source:
            src_key = entry.get("source_key", "")
            urls = (entry.get("news_urls") or [])[:limit_news_per_source]
            items: list[RunNewsOut] = []
            for url in urls:
                n = news_by_url.get(url)
                if n is None:
                    continue
                items.append(RunNewsOut(
                    id=n.id,
                    source_key=n.source_key,
                    title=n.title,
                    url=n.url,
                    published_at=n.published_at,
                    tickers_mentioned=list(n.tickers_mentioned or []),
                    enriched=bool(n.enriched_at),
                ))
            meta = source_meta.get(src_key)
            out.append(RunSourceOut(
                source_key=src_key,
                source_name=meta.name if meta else None,
                source_type=meta.type if meta else None,
                news_count=int(entry.get("news_count") or 0),
                quotes_count=int(entry.get("quotes_count") or 0),
                new_news_count=int(entry.get("new_news_count") or 0),
                errors=list(entry.get("errors") or []),
                news=items,
            ))
        return out


# --- SSE stream -----------------------------------------------------------

def _sse_format(payload: dict) -> str:
    """Formate un event au format SSE (`data: ...\\n\\n`)."""
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _authenticate_stream(request: Request, token: str | None) -> None:
    """Auth du stream SSE.

    EventSource ne peut pas envoyer de headers custom, donc on accepte le token
    en `?token=...` (admin_api_token **ou** JWT de session). Fallback sur
    l'auth header classique pour les cas curl/tests.
    Lève HTTPException 401 si rien ne valide.
    """
    import hmac
    settings = get_settings()

    # 1. Admin token via query-string (bypass super-admin).
    if token and settings.admin_api_token and hmac.compare_digest(token, settings.admin_api_token):
        return

    # 2. JWT de session via query-string.
    if token:
        try:
            payload = decode_session_jwt(token)
        except InvalidSessionError:
            payload = None
        if payload:
            uid = payload.get("uid")
            if uid is not None:
                with get_session() as s:
                    user = s.get(User, uid)
                    if user and user.enabled:
                        return

    # 3. Fallback header-based (Authorization Bearer / X-Admin-Token).
    from .auth import current_user
    current_user(request)  # lève HTTPException si invalide


@stream_router.get("/{run_id}/stream")
async def stream_run(run_id: int, request: Request, token: str | None = None):
    """Stream SSE des événements d'un run. Auth via `?token=` (JWT ou admin)."""
    _authenticate_stream(request, token)

    # Vérifie que le run existe (rapide, on ne garde pas la session ouverte)
    with get_session() as s:
        run = s.get(PipelineRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run introuvable")

    q, history = events.subscribe(run_id)

    async def event_generator():
        try:
            # 1. Replay de l'historique pour les clients qui se connectent
            # après le début du run.
            for evt in history:
                if await request.is_disconnected():
                    return
                yield _sse_format(evt)

            # 2. Stream temps-réel + heartbeat tous les 15s.
            while True:
                if await request.is_disconnected():
                    return
                try:
                    evt = await asyncio.to_thread(q.get, True, 15)
                except queue_mod.Empty:
                    # Heartbeat — évite que nginx / le proxy coupe après 30s.
                    yield ": ping\n\n"
                    continue
                yield _sse_format(evt)
                if evt.get("event") == "run.closed":
                    return
        finally:
            events.unsubscribe(run_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx : désactive le buffering SSE
        },
    )
