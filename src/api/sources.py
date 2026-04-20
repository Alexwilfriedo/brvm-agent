"""Endpoints admin pour gérer les sources (CRUD)."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..database import get_session
from ..models import Source
from .deps import require_admin

router = APIRouter(prefix="/api/sources", tags=["sources"], dependencies=[Depends(require_admin)])


class SourceIn(BaseModel):
    key: str
    name: str
    type: str
    url: str
    enabled: bool = True
    config: dict = {}


class SourcePatch(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None
    config: dict | None = None


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


@router.get("", response_model=list[SourceOut])
def list_sources():
    with get_session() as s:
        return s.execute(select(Source).order_by(Source.id)).scalars().all()


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
        return src


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
        return src


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int):
    with get_session() as s:
        src = s.get(Source, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source introuvable")
        s.delete(src)
