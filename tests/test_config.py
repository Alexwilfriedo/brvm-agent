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
