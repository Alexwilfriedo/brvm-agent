"""Tests du module src/storage — InMemoryStorage et registry.

Le backend S3 (boto3) n'est pas testé ici (nécessiterait moto ou un MinIO
dans un container). On se contente de vérifier l'interface abstraite et
l'injection pour tests.
"""
from __future__ import annotations

import pytest


@pytest.mark.unit
class TestInMemoryStorage:
    def test_put_get_round_trip(self):
        from src.storage import InMemoryStorage

        s = InMemoryStorage()
        s.put_object("foo/bar.pdf", b"hello world")
        assert s.get_object("foo/bar.pdf") == b"hello world"

    def test_put_overwrites(self):
        from src.storage import InMemoryStorage

        s = InMemoryStorage()
        s.put_object("k", b"v1")
        s.put_object("k", b"v2")
        assert s.get_object("k") == b"v2"

    def test_get_missing_raises(self):
        from src.storage import InMemoryStorage, StorageError

        s = InMemoryStorage()
        with pytest.raises(StorageError):
            s.get_object("nope")

    def test_delete_is_idempotent(self):
        from src.storage import InMemoryStorage

        s = InMemoryStorage()
        s.put_object("k", b"v")
        s.delete_object("k")
        s.delete_object("k")  # ne doit pas lever

    def test_delete_prefix_removes_matching(self):
        from src.storage import InMemoryStorage

        s = InMemoryStorage()
        s.put_object("job/1/a.pdf", b"a")
        s.put_object("job/1/b.pdf", b"b")
        s.put_object("job/2/c.pdf", b"c")
        deleted = s.delete_prefix("job/1/")
        assert deleted == 2
        # Les clés du prefix 2 restent
        assert s.get_object("job/2/c.pdf") == b"c"

    def test_delete_prefix_no_match_returns_zero(self):
        from src.storage import InMemoryStorage

        s = InMemoryStorage()
        s.put_object("x", b"x")
        assert s.delete_prefix("nonexistent/") == 0

    def test_ensure_bucket_no_op_in_memory(self):
        """Le backend in-memory n'a pas de bucket — ensure_bucket est un no-op."""
        from src.storage import InMemoryStorage

        s = InMemoryStorage()
        assert s.ensure_bucket() is False


@pytest.mark.unit
class TestStorageRegistry:
    def test_set_for_tests_returns_injected(self):
        from src.storage import InMemoryStorage, get_storage, reset_storage, set_storage_for_tests

        reset_storage()
        mem = InMemoryStorage()
        set_storage_for_tests(mem)
        assert get_storage() is mem

    def test_reset_clears_singleton(self):
        from src.storage import (
            InMemoryStorage,
            get_storage,
            reset_storage,
            set_storage_for_tests,
        )

        set_storage_for_tests(InMemoryStorage())
        reset_storage()
        # Après reset, get_storage() tenterait de construire S3Storage et lèverait
        # StorageNotConfigured sans settings s3_bucket. On valide juste l'état
        # en ré-injectant.
        mem2 = InMemoryStorage()
        set_storage_for_tests(mem2)
        assert get_storage() is mem2
