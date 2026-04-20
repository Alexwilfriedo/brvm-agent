"""Génération et validation des magic links + JWT de session.

Design :
- Magic link : token aléatoire URL-safe envoyé par email. On stocke son **hash**
  SHA-256 en DB (pas le jeton clair) + expiry + consumed flag. Usage unique.
- Session : JWT signé HMAC-SHA256 avec `effective_jwt_secret` de Settings.
  Claims : `sub` (email), `uid` (user_id), `iat`, `exp`, `typ="session"`.

Pas de refresh token — le JWT dure `jwt_expires_days` (défaut 7j), après quoi
l'utilisateur redemande un magic link.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from ..config import get_settings


# --- Magic link token ------------------------------------------------------

def generate_magic_token() -> tuple[str, str]:
    """Génère un token aléatoire URL-safe + son hash SHA-256.

    Le token clair est envoyé dans l'email, le hash est stocké en DB.
    """
    raw = secrets.token_urlsafe(48)
    hashed = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, hashed


def hash_magic_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- JWT de session --------------------------------------------------------

JWT_ALGORITHM = "HS256"


def create_session_jwt(user_id: int, email: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": email,
        "uid": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.jwt_expires_days)).timestamp()),
        "typ": "session",
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=JWT_ALGORITHM)


class InvalidSessionError(Exception):
    """JWT invalide, expiré ou malformé."""


def decode_session_jwt(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.effective_jwt_secret, algorithms=[JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as e:
        raise InvalidSessionError("Session expirée — redemande un lien de connexion.") from e
    except jwt.InvalidTokenError as e:
        raise InvalidSessionError("Session invalide.") from e
    if payload.get("typ") != "session":
        raise InvalidSessionError("Type de jeton incorrect.")
    return payload
