"""Fixtures partagées pytest.

L'import de `src.config` déclenche une validation Pydantic — on injecte
des valeurs d'env minimales pour que `Settings()` passe sans secret réel.
"""
import os

import pytest

# Variables d'env minimales pour éviter le ValidationError au chargement de Settings
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("BREVO_SMTP_USER", "test@smtp-brevo.com")
os.environ.setdefault("BREVO_SMTP_PASSWORD", "test-pw")
os.environ.setdefault("EMAIL_FROM", "test@example.ci")
os.environ.setdefault("EMAIL_TO", "dest@example.ci")
os.environ.setdefault("ADMIN_API_TOKEN", "test-token-with-at-least-24-chars-to-pass-validator")


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Invalide le cache `get_settings()` entre tests pour pouvoir override des env vars."""
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
