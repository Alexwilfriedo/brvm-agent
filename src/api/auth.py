"""Endpoints d'authentification par magic link.

Flow :
  1. POST /api/auth/request-link { email }  →  envoie un email si whitelist
  2. POST /api/auth/verify        { token } →  valide → JWT + user payload
  3. GET  /api/auth/me                      →  profile de la session courante
  4. POST /api/auth/logout                  →  côté client (on stocke le JWT client-side)

Sécurité :
- Réponses **non énumératives** : /request-link renvoie 200 même si l'email
  n'est pas whitelisté (éviter de divulguer qui a accès).
- Rate limit : 5 requêtes/email/heure.
- Magic link usage unique, TTL 15 min.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from ..auth.email import send_magic_link
from ..auth.rate_limit import RateLimitExceeded, check_rate_limit
from ..auth.tokens import (
    InvalidSessionError,
    create_session_jwt,
    decode_session_jwt,
    generate_magic_token,
    hash_magic_token,
)
from ..config import get_settings
from ..database import get_session
from ..models import LoginToken, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --- Schemas ---------------------------------------------------------------

class RequestLinkIn(BaseModel):
    email: EmailStr


class VerifyIn(BaseModel):
    token: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str | None
    enabled: bool


class SessionOut(BaseModel):
    jwt: str
    expires_days: int
    user: UserOut


# --- Helpers ---------------------------------------------------------------

def _client_ip(request: Request) -> str:
    # Respecte X-Forwarded-For derrière Railway / proxy
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# --- Routes ----------------------------------------------------------------

@router.post("/request-link", status_code=status.HTTP_200_OK)
def request_magic_link(body: RequestLinkIn, request: Request):
    """Génère un magic link + envoie par email SI l'email est whitelisté.

    Toujours 200 pour éviter la divulgation des users whitelisted.
    """
    settings = get_settings()
    email = body.email.lower().strip()

    with get_session() as s:
        user = s.execute(
            select(User).where(User.email == email).where(User.enabled.is_(True))
        ).scalar_one_or_none()

        # Rate limit — appliqué avant même de vérifier l'user (évite les timing attacks)
        try:
            check_rate_limit(s, email)
        except RateLimitExceeded as e:
            logger.warning(f"Rate limit dépassé pour {email}: {e}")
            # On renvoie quand même 200 pour ne pas signaler qu'on connaît l'email
            return {"status": "ok"}

        if not user:
            logger.info(f"Magic link demandé pour email non-whitelisté : {email}")
            return {"status": "ok"}

        raw, hashed = generate_magic_token()
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.magic_link_ttl_minutes)

        s.add(LoginToken(
            email=email,
            token_hash=hashed,
            expires_at=expires_at,
            requested_ip=_client_ip(request)[:64],
            requested_ua=(request.headers.get("user-agent") or "")[:255],
        ))

    link = f"{settings.frontend_url.rstrip('/')}/auth/verify?token={raw}"
    try:
        send_magic_link(
            to_email=email,
            link=link,
            ttl_minutes=settings.magic_link_ttl_minutes,
            name=user.name,
            ip=_client_ip(request),
        )
    except Exception:
        logger.exception(f"Échec envoi magic link à {email}")
        # On lève une 500 parce que l'email est le canal critique — sans envoi,
        # le user ne peut pas se logger. Mieux vaut signaler clairement que masquer.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Échec envoi de l'email. Réessaie dans une minute.",
        ) from None

    return {"status": "ok"}


@router.post("/verify", response_model=SessionOut)
def verify_magic_link(body: VerifyIn):
    """Consomme le magic link et émet un JWT de session."""
    settings = get_settings()
    token_hash = hash_magic_token(body.token.strip())

    with get_session() as s:
        lt = s.execute(
            select(LoginToken).where(LoginToken.token_hash == token_hash)
        ).scalar_one_or_none()

        if not lt:
            raise HTTPException(status_code=400, detail="Lien invalide ou inconnu.")
        if lt.consumed_at is not None:
            raise HTTPException(status_code=400, detail="Lien déjà utilisé.")

        # Comparaison aware/naive safe
        now = datetime.now(UTC)
        expiry = lt.expires_at if lt.expires_at.tzinfo else lt.expires_at.replace(tzinfo=UTC)
        if now > expiry:
            raise HTTPException(status_code=400, detail="Lien expiré — redemande un nouveau lien.")

        user = s.execute(
            select(User).where(User.email == lt.email).where(User.enabled.is_(True))
        ).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=403, detail="Utilisateur désactivé ou supprimé.")

        lt.consumed_at = now
        user.last_login_at = now
        s.flush()

        jwt_token = create_session_jwt(user_id=user.id, email=user.email)
        user_out = UserOut(id=user.id, email=user.email, name=user.name, enabled=user.enabled)

    return SessionOut(
        jwt=jwt_token,
        expires_days=settings.jwt_expires_days,
        user=user_out,
    )


# --- Session deps ----------------------------------------------------------

def _extract_bearer(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


def current_user(request: Request) -> UserOut:
    """Dépendance FastAPI — renvoie le UserOut courant ou 401.

    Accepte :
      - `Authorization: Bearer <jwt>` (session utilisateur)
      - `X-Admin-Token: <admin_api_token>` (bypass super-admin, casse-de-verre)
    """
    settings = get_settings()

    # Bypass admin token
    admin_token = request.headers.get("x-admin-token")
    if admin_token and admin_token == settings.admin_api_token:
        return UserOut(
            id=0,
            email="super-admin@brvm-agent.local",
            name="Super Admin (token)",
            enabled=True,
        )

    # JWT session
    jwt_token = _extract_bearer(request)
    if not jwt_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentification requise",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_session_jwt(jwt_token)
    except InvalidSessionError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    with get_session() as s:
        user = s.get(User, payload["uid"])
        if not user or not user.enabled:
            raise HTTPException(status_code=403, detail="Utilisateur désactivé.")
        return UserOut(id=user.id, email=user.email, name=user.name, enabled=user.enabled)


@router.get("/me", response_model=UserOut)
def me(user: Annotated[UserOut, Depends(current_user)]):
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    """Logout côté serveur — no-op. Le client doit purger son JWT.

    Pas de blacklist JWT pour l'instant (complexité > bénéfice pour usage perso).
    Si fuite du JWT, rotate `JWT_SECRET` → invalide tous les tokens émis.
    """
    return
