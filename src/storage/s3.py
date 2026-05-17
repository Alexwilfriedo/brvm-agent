"""Backend S3-compatible (MinIO / Railway / AWS S3) via boto3.

Config lue depuis `Settings` :
  - `s3_endpoint_url` (optionnel — vide = AWS S3 par région)
  - `s3_region`, `s3_access_key_id`, `s3_secret_access_key`
  - `s3_bucket` (requis)
  - `s3_prefix` (optionnel — préfixe dans le bucket)

Décisions de design :
  - **Chemin de style virtuel-host** : par défaut boto3 utilise `vhost` style.
    MinIO requiert `path` style pour être compatible (sinon "bucket.localhost").
    Détection auto : si `endpoint_url` contient `localhost` ou `127.0.0.1`, on
    force `path` style.
  - **No retry custom** : boto3 retry par défaut (3 tentatives, backoff expo).
  - **Content-type** : passé tel quel si fourni, sinon `application/octet-stream`.
"""
from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from ..config import get_settings
from .base import Storage, StorageError, StorageNotConfigured

logger = logging.getLogger(__name__)


class S3Storage(Storage):
    """Backend S3-compatible."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        client: Any,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._client = client

    # --- Factories ---------------------------------------------------------

    @classmethod
    def from_settings(cls) -> "S3Storage":
        """Instancie depuis `Settings`. Lève `StorageNotConfigured` si bucket absent."""
        s = get_settings()
        if not s.s3_bucket:
            raise StorageNotConfigured(
                "S3_BUCKET manquant. Configure les variables d'environnement "
                "S3_BUCKET + S3_ACCESS_KEY_ID + S3_SECRET_ACCESS_KEY "
                "(+ S3_ENDPOINT_URL pour MinIO)."
            )

        # Style adressage : MinIO local a besoin de 'path', AWS S3 supporte les deux.
        endpoint = (s.s3_endpoint_url or "").lower()
        use_path_style = any(h in endpoint for h in ("localhost", "127.0.0.1", "minio"))
        boto_cfg = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if use_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        )

        client_kwargs: dict[str, Any] = {
            "region_name": s.s3_region or "us-east-1",
            "config": boto_cfg,
        }
        if s.s3_endpoint_url:
            client_kwargs["endpoint_url"] = s.s3_endpoint_url
        if s.s3_access_key_id and s.s3_secret_access_key:
            client_kwargs["aws_access_key_id"] = s.s3_access_key_id
            client_kwargs["aws_secret_access_key"] = s.s3_secret_access_key

        client = boto3.client("s3", **client_kwargs)
        return cls(
            bucket=s.s3_bucket,
            prefix=(s.s3_prefix or "").lstrip("/"),
            client=client,
        )

    # --- Helpers -----------------------------------------------------------

    def _full_key(self, key: str) -> str:
        """Compose la clé réelle avec le préfixe configuré."""
        if self._prefix:
            return f"{self._prefix.rstrip('/')}/{key.lstrip('/')}"
        return key.lstrip("/")

    # --- Storage interface -------------------------------------------------

    def put_object(self, key: str, content: bytes, *, content_type: str | None = None) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._full_key(key),
                Body=content,
                ContentType=content_type or "application/octet-stream",
            )
        except ClientError as e:
            raise StorageError(f"put_object({key!r}) a échoué : {e}") from e

    def get_object(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
            return resp["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404"}:
                raise StorageError(f"Key introuvable : {key!r}") from e
            raise StorageError(f"get_object({key!r}) a échoué : {e}") from e

    def delete_object(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._full_key(key))
        except ClientError as e:
            # delete est idempotent — on log mais on ne lève pas si déjà absent
            logger.warning(f"delete_object({key!r}) : {e}")

    def ensure_bucket(self) -> bool:
        """Crée le bucket si absent. True si créé, False si existait déjà.

        Spec S3 : `create_bucket` requiert `CreateBucketConfiguration={'LocationConstraint': region}`
        pour toute région ≠ `us-east-1`. MinIO accepte les deux formes mais on
        respecte le contrat AWS pour rester portable. `BucketAlreadyOwnedByYou`
        et `BucketAlreadyExists` → no-op (idempotent)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return False  # déjà là
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code not in {"404", "NoSuchBucket"} and status != 404:
                # Autre erreur (403, mauvais creds…) → on remonte tel quel
                raise StorageError(
                    f"head_bucket({self._bucket!r}) a échoué : {e}",
                ) from e
            # 404 → on tente la création

        region = (self._client.meta.region_name or "us-east-1")
        create_kwargs: dict[str, Any] = {"Bucket": self._bucket}
        if region != "us-east-1":
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            self._client.create_bucket(**create_kwargs)
            logger.info(f"[storage] Bucket {self._bucket!r} créé (region={region}).")
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                return False
            raise StorageError(
                f"create_bucket({self._bucket!r}) a échoué : {e}",
            ) from e

    def delete_prefix(self, prefix: str) -> int:
        """List + delete en batch de 1000 (limite S3)."""
        full_prefix = self._full_key(prefix).rstrip("/") + "/"
        deleted = 0
        continuation = None
        while True:
            list_kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": full_prefix,
                "MaxKeys": 1000,
            }
            if continuation:
                list_kwargs["ContinuationToken"] = continuation
            try:
                page = self._client.list_objects_v2(**list_kwargs)
            except ClientError as e:
                raise StorageError(f"list_objects_v2({full_prefix!r}) : {e}") from e
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", []) or []]
            if keys:
                try:
                    self._client.delete_objects(
                        Bucket=self._bucket,
                        Delete={"Objects": keys, "Quiet": True},
                    )
                    deleted += len(keys)
                except ClientError as e:
                    raise StorageError(f"delete_objects : {e}") from e
            if not page.get("IsTruncated"):
                break
            continuation = page.get("NextContinuationToken")
        return deleted
