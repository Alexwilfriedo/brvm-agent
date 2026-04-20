"""Configuration centralisée (lue depuis les variables d'environnement)."""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str
    model_enrichment: str = "claude-sonnet-4-6"
    model_synthesis: str = "claude-opus-4-7"

    # Database
    database_url: str

    # Brevo SMTP
    brevo_smtp_host: str = "smtp-relay.brevo.com"
    brevo_smtp_port: int = 587
    brevo_smtp_user: str
    brevo_smtp_password: str
    email_from: str
    email_from_name: str = "BRVM Agent"
    email_to: str

    # Brevo WhatsApp (optionnel — ship email-first, WhatsApp plus tard)
    brevo_api_key: str = ""
    whatsapp_sender_number: str = ""
    whatsapp_to_number: str = ""
    whatsapp_template_id: str = ""

    # Scheduler
    timezone: str = "Africa/Abidjan"
    default_cron: str = "0 8 * * *"

    # Profil investisseur
    investor_profile: str = (
        "Long terme (3-5 ans) orienté dividendes et value. "
        "Secteurs préférés : bancaire, télécoms, industrie. "
        "Éviter titres illiquides (volume < 500/jour). Tolérance risque modérée."
    )

    # Admin — OBLIGATOIRE en prod, aucun default de secours
    admin_api_token: str

    # Observabilité (optionnel)
    sentry_dsn: str = ""
    sentry_environment: str = "production"
    sentry_traces_sample_rate: float = 0.1

    # Misc
    log_level: str = "INFO"
    news_lookback_hours: int = 36

    @field_validator("admin_api_token")
    @classmethod
    def _reject_placeholder_token(cls, v: str) -> str:
        if not v or v.strip() in {"change-me", "change-me-to-a-long-random-string"}:
            raise ValueError(
                "ADMIN_API_TOKEN doit être défini avec une valeur non-placeholder. "
                "Génère un token aléatoire long (ex: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`)."
            )
        if len(v) < 24:
            raise ValueError("ADMIN_API_TOKEN trop court (minimum 24 caractères).")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
