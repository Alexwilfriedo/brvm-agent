"""Endpoints admin pour gérer les sources (CRUD)."""
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import String, cast, select

from ..database import get_session
from ..models import Source
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, PaginatedResponse, ilike_any, paginate

router = APIRouter(prefix="/api/sources", tags=["sources"], dependencies=[Depends(require_admin)])


def _validate_source_url(url: str) -> str:
    """Bloque les URLs dangereuses post-compromise admin.

    Sans ça, un attaquant avec un admin_token peut créer une source
    `file:///etc/passwd` ou `http://169.254.169.254/...` (metadata Railway)
    et lire son contenu via le prochain run collecteur. On whitelist
    strictement http/https et on rejette les IPs RFC1918 / link-local.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Schéma d'URL non autorisé (http ou https uniquement).")
    host = (parsed.hostname or "").lower()
    # Blocklist des IP metadata / local (SSRF classique)
    blocked_prefixes = ("169.254.", "127.", "localhost", "metadata.")
    if any(host == p.rstrip(".") or host.startswith(p) for p in blocked_prefixes):
        raise ValueError(f"Hôte bloqué (SSRF): {host}")
    return url


class SourceIn(BaseModel):
    key: str
    name: str
    type: str
    url: str
    enabled: bool = True
    config: dict = {}

    @field_validator("url")
    @classmethod
    def _url_safe(cls, v: str) -> str:
        return _validate_source_url(v)


class SourcePatch(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None
    config: dict | None = None

    @field_validator("url")
    @classmethod
    def _url_safe(cls, v: str | None) -> str | None:
        return _validate_source_url(v) if v is not None else v


class SourceOut(BaseModel):
    id: int
    key: str
    name: str
    type: str
    url: str
    enabled: bool
    config: dict
    last_collected_at: datetime | None
    last_status: str | None
    last_error: str | None

    class Config:
        from_attributes = True


@router.get("", response_model=PaginatedResponse[SourceOut])
def list_sources(
    q: str | None = Query(None, description="Recherche fuzzy dans key/name/url/type"),
    enabled: bool | None = None,
    type: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with get_session() as s:
        stmt = select(Source).order_by(Source.id)
        if enabled is not None:
            stmt = stmt.where(Source.enabled.is_(enabled))
        if type:
            stmt = stmt.where(Source.type == type)
        if q:
            stmt = stmt.where(
                ilike_any([
                    cast(Source.key, String),
                    cast(Source.name, String),
                    cast(Source.url, String),
                    cast(Source.type, String),
                ], q)
            )
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[SourceOut](
            items=[SourceOut.model_validate(r) for r in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.post("", response_model=SourceOut, status_code=201)
def create_source(body: SourceIn):
    with get_session() as s:
        existing = s.execute(select(Source).where(Source.key == body.key)).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=409, detail=f"Source '{body.key}' existe déjà")
        src = Source(**body.model_dump())
        s.add(src)
        s.flush()
        s.refresh(src)
        # Validation Pydantic **dans** la session : sinon l'ORM instance devient
        # détachée dès la sortie du `with` et FastAPI plante avec DetachedInstanceError
        # au moment de sérialiser la réponse.
        return SourceOut.model_validate(src)


@router.patch("/{source_id}", response_model=SourceOut)
def patch_source(source_id: int, body: SourcePatch):
    with get_session() as s:
        src = s.get(Source, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source introuvable")
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(src, field, value)
        s.flush()
        s.refresh(src)
        return SourceOut.model_validate(src)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int):
    with get_session() as s:
        src = s.get(Source, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source introuvable")
        s.delete(src)
