"""Validation des garde-fous de configuration."""
import pytest
from pydantic import ValidationError


@pytest.mark.unit
class TestAdminToken:
    def test_rejects_placeholder(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "change-me")
        from src.config import Settings

        with pytest.raises(ValidationError, match="placeholder"):
            Settings()

    def test_rejects_too_short(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "short")
        from src.config import Settings

        with pytest.raises(ValidationError, match="trop court"):
            Settings()

    def test_rejects_empty(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "")
        from src.config import Settings

        with pytest.raises(ValidationError):
            Settings()

    def test_accepts_valid_token(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 32)
        from src.config import Settings

        settings = Settings()
        assert settings.admin_api_token == "a" * 32


@pytest.mark.unit
class TestJwtSecret:
    """E-1 (ADR-003) : JWT_SECRET obligatoire, ≥ 32 chars, distinct du admin token."""

    def test_rejects_empty(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 32)
        monkeypatch.setenv("JWT_SECRET", "")
        from src.config import Settings

        with pytest.raises(ValidationError, match="JWT_SECRET"):
            Settings()

    def test_rejects_placeholder(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 32)
        monkeypatch.setenv("JWT_SECRET", "change-me")
        from src.config import Settings

        with pytest.raises(ValidationError, match="placeholder|JWT_SECRET"):
            Settings()

    def test_rejects_too_short(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 32)
        monkeypatch.setenv("JWT_SECRET", "x" * 20)
        from src.config import Settings

        with pytest.raises(ValidationError, match="trop court"):
            Settings()

    def test_rejects_equal_to_admin_token(self, monkeypatch):
        """Garde-fou ADR-003 : JWT_SECRET != ADMIN_API_TOKEN."""
        token = "a" * 40
        monkeypatch.setenv("ADMIN_API_TOKEN", token)
        monkeypatch.setenv("JWT_SECRET", token)
        from src.config import Settings

        with pytest.raises(ValidationError, match="DISTINCT"):
            Settings()

    def test_accepts_valid_distinct_secret(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 32)
        monkeypatch.setenv("JWT_SECRET", "b" * 48)
        from src.config import Settings

        settings = Settings()
        assert settings.jwt_secret == "b" * 48
        assert settings.effective_jwt_secret == settings.jwt_secret
