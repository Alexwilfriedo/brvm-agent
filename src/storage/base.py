"""Interface storage + registry + in-memory fallback pour les tests.

Séparé de `s3.py` pour permettre au code métier d'importer `get_storage()`
sans tirer boto3 dans les tests unitaires.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from threading import Lock

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Erreur durable (upload raté, objet introuvable, etc.)."""


class StorageNotConfigured(StorageError):
    """S3 n'est pas configuré — `S3_BUCKET` et credentials manquants.

    Levé par les call-sites qui nécessitent un bucket (upload backfill) pour
    renvoyer un 503 explicite plutôt qu'un crash au 1er put_object.
    """


# --- Abstract interface -----------------------------------------------------


class Storage(ABC):
    """Contrat minimal pour un backend de stockage objet."""

    @abstractmethod
    def put_object(self, key: str, content: bytes, *, content_type: str | None = None) -> None:
        """Upload un blob sous la clé `key`. Écrase toute version existante.

        Raises:
            StorageError: échec réseau, bucket inexistant, credentials invalides.
        """

    @abstractmethod
    def get_object(self, key: str) -> bytes:
        """Télécharge le contenu de `key`.

        Raises:
            StorageError: `key` n'existe pas ou erreur réseau.
        """

    @abstractmethod
    def delete_object(self, key: str) -> None:
        """Supprime `key`. Idempotent : pas d'erreur si la clé n'existe déjà plus."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Supprime toutes les clés sous `prefix`. Retourne le nb d'objets supprimés.

        Utilisé pour nettoyer un job de backfill annulé d'un coup.
        """

    @abstractmethod
    def ensure_bucket(self) -> bool:
        """Crée le bucket s'il n'existe pas. Idempotent. Retourne True si créé,
        False si déjà présent. Lève `StorageError` sur erreur de permission."""


# --- In-memory implementation (tests) ---------------------------------------


class InMemoryStorage(Storage):
    """Implémentation triviale backée par un dict — zéro dépendance, pour tests."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put_object(self, key: str, content: bytes, *, content_type: str | None = None) -> None:
        self._data[key] = bytes(content)

    def get_object(self, key: str) -> bytes:
        if key not in self._data:
            raise StorageError(f"Key absente : {key!r}")
        return self._data[key]

    def delete_object(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        to_delete = [k for k in self._data if k.startswith(prefix)]
        for k in to_delete:
            del self._data[k]
        return len(to_delete)

    def ensure_bucket(self) -> bool:
        # Le backend in-memory n'a pas la notion de bucket.
        return False

    # Exposé pour les tests — utile pour inspecter l'état du mock.
    def _keys(self) -> list[str]:
        return list(self._data.keys())


# --- Registry ---------------------------------------------------------------

_storage_instance: Storage | None = None
_storage_lock = Lock()


def get_storage() -> Storage:
    """Retourne le singleton `Storage` configuré depuis `Settings`.

    Instancie un `S3Storage` au 1er appel. Pour les tests, préférer
    `set_storage_for_tests(InMemoryStorage())` avant le 1er call.

    Raises:
        StorageNotConfigured: aucune config S3 n'a été fournie.
    """
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance
    with _storage_lock:
        if _storage_instance is not None:
            return _storage_instance
        # Import tardif pour ne pas tirer boto3 dans les tests qui injectent
        # un InMemoryStorage.
        from .s3 import S3Storage

        _storage_instance = S3Storage.from_settings()
        return _storage_instance


def set_storage_for_tests(storage: Storage) -> None:
    """Injecte un backend (typiquement `InMemoryStorage`) pour les tests."""
    global _storage_instance
    with _storage_lock:
        _storage_instance = storage


def reset_storage() -> None:
    """Vide le singleton — à appeler entre deux tests pour isoler."""
    global _storage_instance
    with _storage_lock:
        _storage_instance = None
