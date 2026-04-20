"""CRUD des utilisateurs autorisés de la console."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..database import get_session
from ..models import User
from .auth import current_user
from .pagination import DEFAULT_LIMIT, PaginatedResponse, ilike_any, paginate

router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(current_user)],
)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str | None
    enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    name: str | None = None
    enabled: bool = True


class UserPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None


@router.get("", response_model=PaginatedResponse[UserOut])
def list_users(
    q: str | None = Query(None, description="Recherche fuzzy sur email/name"),
    enabled: bool | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with get_session() as s:
        stmt = select(User).order_by(User.created_at.desc())
        if enabled is not None:
            stmt = stmt.where(User.enabled.is_(enabled))
        if q:
            stmt = stmt.where(ilike_any([User.email, User.name], q))
        items, total = paginate(s, stmt, limit=limit, offset=offset)
        return PaginatedResponse[UserOut](
            items=[UserOut.model_validate(u) for u in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate):
    with get_session() as s:
        user = User(
            email=body.email.lower().strip(),
            name=body.name.strip() if body.name else None,
            enabled=body.enabled,
        )
        s.add(user)
        try:
            s.flush()
        except IntegrityError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Un utilisateur avec l'email {body.email} existe déjà.",
            ) from e
        return UserOut.model_validate(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserPatch):
    with get_session() as s:
        user = s.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        if body.name is not None:
            user.name = body.name or None
        if body.enabled is not None:
            user.enabled = body.enabled
        s.flush()
        return UserOut.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, me: UserOut = Depends(current_user)):
    if user_id == me.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu ne peux pas te supprimer toi-même.",
        )
    with get_session() as s:
        user = s.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        s.delete(user)
    return


# Expose le UserOut du module auth pour les autres routers
__all__ = ["router", "UserOut"]


# Dernière connexion toujours à jour (non, c'est dans /auth/verify)
_ = UTC  # prevent unused import
