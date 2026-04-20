"""CRUD des destinataires (email, WhatsApp).

Protection admin obligatoire : POSSIBILITÉ D'EXFILTRATION si l'endpoint est
ouvert, on exige `X-Admin-Token` comme partout ailleurs.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import String, cast, select
from sqlalchemy.exc import IntegrityError

from ..database import get_session
from ..models import Recipient
from .deps import require_admin
from .pagination import DEFAULT_LIMIT, PaginatedResponse, ilike_any, paginate

router = APIRouter(
    prefix="/api/recipients",
    tags=["recipients"],
    dependencies=[Depends(require_admin)],
)

Channel = Literal["email", "whatsapp"]

# Validateurs simples — on veut attraper les fautes de frappe, pas faire du RFC 5322
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _validate_address(channel: str, address: str) -> str:
    address = address.strip()
    if channel == "email" and not _EMAIL_RE.match(address):
        raise ValueError(f"Adresse email invalide : {address!r}")
    if channel == "whatsapp" and not _E164_RE.match(address):
        raise ValueError(
            f"Numéro WhatsApp doit être au format E.164 (ex: +2250700000000) — reçu : {address!r}"
        )
    return address


# --- Schemas ----------------------------------------------------------------

class RecipientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel: str
    address: str
    name: str | None
    enabled: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class RecipientCreate(BaseModel):
    channel: Channel
    address: str
    name: str | None = None
    enabled: bool = True
    notes: str | None = None

    @field_validator("address")
    @classmethod
    def _norm(cls, v: str, info) -> str:
        channel = info.data.get("channel")
        if not channel:
            return v.strip()
        return _validate_address(channel, v)


class RecipientPatch(BaseModel):
    address: str | None = None
    name: str | None = None
    enabled: bool | None = None
    notes: str | None = None


# --- Routes -----------------------------------------------------------------

@router.get("", response_model=PaginatedResponse[RecipientOut])
def list_recipients(
    q: str | None = Query(None, description="Recherche fuzzy dans address/name/notes"),
    channel: Channel | None = None,
    enabled: bool | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with get_session() as s:
        stmt = select(Recipient).order_by(Recipient.channel, Recipient.id)
        if channel is not None:
            stmt = stmt.where(Recipient.channel == channel)
        if enabled is not None:
            stmt = stmt.where(Recipient.enabled.is_(enabled))
        if q:
            stmt = stmt.where(
                ilike_any([
                    cast(Recipient.address, String),
                    cast(Recipient.name, String),
                    cast(Recipient.notes, String),
                ], q)
            )
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[RecipientOut](
            items=[RecipientOut.model_validate(r) for r in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.post("", response_model=RecipientOut, status_code=status.HTTP_201_CREATED)
def create_recipient(body: RecipientCreate):
    with get_session() as s:
        r = Recipient(
            channel=body.channel,
            address=body.address,
            name=body.name,
            enabled=body.enabled,
            notes=body.notes,
        )
        s.add(r)
        try:
            s.flush()
        except IntegrityError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Recipient {body.channel}:{body.address} existe déjà.",
            ) from e
        s.refresh(r)
        return RecipientOut.model_validate(r)


@router.patch("/{recipient_id}", response_model=RecipientOut)
def update_recipient(recipient_id: int, body: RecipientPatch):
    with get_session() as s:
        r = s.get(Recipient, recipient_id)
        if not r:
            raise HTTPException(status_code=404, detail="Recipient introuvable")
        if body.address is not None:
            r.address = _validate_address(r.channel, body.address)
        if body.name is not None:
            r.name = body.name
        if body.enabled is not None:
            r.enabled = body.enabled
        if body.notes is not None:
            r.notes = body.notes
        r.updated_at = datetime.now(UTC)
        try:
            s.flush()
        except IntegrityError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflit d'unicité (channel + address).",
            ) from e
        s.refresh(r)
        return RecipientOut.model_validate(r)


@router.delete("/{recipient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipient(recipient_id: int):
    with get_session() as s:
        r = s.get(Recipient, recipient_id)
        if not r:
            raise HTTPException(status_code=404, detail="Recipient introuvable")
        s.delete(r)
    return
