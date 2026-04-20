"""Dépendance d'auth unifiée.

Toutes les routes admin passent par `current_user` qui accepte :
  - `Authorization: Bearer <jwt>` (session utilisateur via magic link)
  - `X-Admin-Token: <admin_api_token>` (bypass super-admin, casse-de-verre)

`require_admin` est conservé comme alias rétro-compat.
"""
from .auth import current_user

require_admin = current_user

__all__ = ["require_admin", "current_user"]
