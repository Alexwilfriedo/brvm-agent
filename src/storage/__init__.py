"""Storage abstraction — par défaut S3-compatible (MinIO / Railway bucket / AWS S3).

Expose :
  - `get_storage()` : récupère le singleton `Storage` configuré depuis `Settings`.
  - `Storage` : interface put/get/delete/delete_prefix. Implémenté par `S3Storage`.
  - `StorageNotConfigured` : raise explicite si on tente d'utiliser le storage
    sans avoir configuré `S3_BUCKET` + credentials.
  - `InMemoryStorage` : implémentation dict pour les tests unitaires (injectée
    via `set_storage_for_tests` quand nécessaire).
"""
from __future__ import annotations

from .base import (
    InMemoryStorage,
    Storage,
    StorageError,
    StorageNotConfigured,
    get_storage,
    reset_storage,
    set_storage_for_tests,
)

__all__ = [
    "InMemoryStorage",
    "Storage",
    "StorageError",
    "StorageNotConfigured",
    "get_storage",
    "reset_storage",
    "set_storage_for_tests",
]
