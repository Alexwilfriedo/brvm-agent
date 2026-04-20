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

    # Brevo SMTP — identité expéditeur + credentials (les destinataires sont en DB, table `recipients`)
    brevo_smtp_host: str = "smtp-relay.brevo.com"
    brevo_smtp_port: int = 587
    brevo_smtp_user: str
    brevo_smtp_password: str
    email_from: str
    email_from_name: str = "BRVM Agent"
    # Seed-only : si défini ET si table `recipients` vide au démarrage, un recipient email est créé.
    # Les destinataires réels sont ensuite gérés via /api/recipients.
    email_to: str = ""

    # Wassoya WhatsApp (destinataires en DB — channel='whatsapp')
    # Laisser wassoya_api_key vide désactive proprement l'envoi WhatsApp.
    # Wassoya impose d'envoyer via TEMPLATE Meta approuvé, pas en texte libre.
    wassoya_api_key: str = ""
    wassoya_api_base_url: str = "https://api.wassoya.com"
    wassoya_sender_number: str = ""   # numéro WhatsApp Business au format "2250700000000" (sans +)
    wassoya_template_name: str = ""   # nom du template Meta approuvé (ex: "brvm_brief_v1")
    # Seed-only : si défini au 1er boot ET recipients vide, crée 1 recipient WA.
    whatsapp_to_number: str = ""

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

    # Auth magic link + JWT session
    # OBLIGATOIRE et DISTINCT de admin_api_token : permet de rotater l'un sans
    # invalider l'autre. Génération : `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    # Voir ADR-003 pour l'historique (ancien mode : dérivation du admin token).
    jwt_secret: str
    jwt_expires_days: int = 7
    magic_link_ttl_minutes: int = 15
    # URL publique du front (lien magique : {frontend_url}/auth/verify?token=...)
    frontend_url: str = "http://localhost:5173"
    # CORS : liste CSV des origines autorisées à appeler l'API (console admin)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Seed-only : si défini ET users vide au 1er boot, crée le 1er admin.
    initial_admin_email: str = ""

    # Observabilité (optionnel)
    sentry_dsn: str = ""
    sentry_environment: str = "production"
    sentry_traces_sample_rate: float = 0.1

    # Misc
    log_level: str = "INFO"
    news_lookback_hours: int = 36

    # --- Propriétés dérivées -------------------------------------------

    @property
    def effective_jwt_secret(self) -> str:
        """JWT secret effectif. Conservé pour compat des call-sites ; renvoie
        désormais toujours `self.jwt_secret` (validé par ailleurs)."""
        return self.jwt_secret

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: str) -> str:
        if not v or v.strip() in {"change-me", "change-me-to-a-long-random-string"}:
            raise ValueError(
                "JWT_SECRET doit être défini avec une valeur non-placeholder, "
                "indépendante de ADMIN_API_TOKEN. Génère : "
                "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        if len(v) < 32:
            raise ValueError("JWT_SECRET trop court (minimum 32 caractères).")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_must_differ_from_admin_token(cls, v: str, info) -> str:
        admin = info.data.get("admin_api_token")
        if admin and v == admin:
            raise ValueError(
                "JWT_SECRET doit être DISTINCT de ADMIN_API_TOKEN — permet de "
                "rotater l'un sans invalider l'autre (voir ADR-003)."
            )
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
