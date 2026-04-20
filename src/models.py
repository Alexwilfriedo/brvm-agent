"""Modèles de données (SQLAlchemy 2.0 typed).

Règle : toutes les colonnes datetime sont `timezone-aware`. Les defaults
utilisent `datetime.now(timezone.utc)` via une `lambda` pour éviter que la
valeur soit capturée au chargement du module.
"""
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    """Default callable — évalué à l'insertion, pas au chargement du module."""
    return datetime.now(UTC)


class Source(Base):
    """Source de données configurée (extensible via UI)."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32))  # "brvm_official" | "rss" | "scraper"
    url: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))   # "ok" | "error"
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Quote(Base):
    """Cotation de clôture journalière sur la BRVM.

    `extras` stocke les métriques détaillées scrapées : open/high/low,
    previous_close, RSI, beta_1y, PER, dividend, market_cap_mfcfa, etc.
    Voir `SikaQuotesCollector._parse_ticker_page` pour la liste exhaustive.
    """
    __tablename__ = "quotes"
    __table_args__ = (
        Index("ix_quotes_ticker_date", "ticker", "quote_date", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(128))
    sector: Mapped[str | None] = mapped_column(String(64))
    # Code pays UEMOA (ci/sn/tg/bj/ml/ne/bf) — utilisé pour l'URL Sika Finance
    country: Mapped[str | None] = mapped_column(String(8))
    quote_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_price: Mapped[float] = mapped_column(Float, default=0.0)
    variation_pct: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    value_traded: Mapped[float] = mapped_column(Float, default=0.0)
    # Métriques détaillées (volatile selon collector)
    extras: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NewsArticle(Base):
    """Article/news récupéré depuis une source."""
    __tablename__ = "news"
    __table_args__ = (
        Index("ix_news_url_unique", "url", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    # Tickers mentionnés (détectés par enrichissement LLM)
    tickers_mentioned: Mapped[list] = mapped_column(JSON, default=list)
    # Enrichissement LLM : sentiment, thèmes, matérialité, impact
    enrichment: Mapped[dict] = mapped_column(JSON, default=dict)
    # Marqueur explicite pour distinguer "jamais tenté" de "tenté et vide"
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Brief(Base):
    """Rapport quotidien — un brief canonique par jour, revisions explicites.

    Règles (pattern C, aligné desks de recherche sell-side) :
    - **1 brief maximum par date**. Contrainte d'unicité sur la date calendaire.
    - Re-run le même jour → incrémente `revision`, met à jour `revised_at` et
      le `payload` textuel. Les `signals` restent gelés à la révision 1
      (intégrité backtest non-négociable).
    - Cron par défaut = idempotent : skip silencieux si brief du jour existe.
    """
    __tablename__ = "briefs"
    # L'unicité par DATE calendaire est enforçée par un index fonctionnel
    # `ix_briefs_brief_date_unique ON briefs ((brief_date::date))` créé par
    # `_INLINE_MIGRATIONS` dans database.py. SQLAlchemy `create_all()` ne
    # sait pas produire ce type d'index, d'où la migration inline.

    id: Mapped[int] = mapped_column(primary_key=True)
    brief_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary_markdown: Mapped[str] = mapped_column(Text)
    # JSON structuré complet produit par Opus (évolue avec les révisions)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # Numéro de révision : 1 au 1er run du jour, 2+ sur re-run explicite
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Timestamp de la dernière révision (null si revision == 1)
    revised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # "pending" | "delivered" | "partial" | "failed" | "failed_synth"
    # (failed_synth = synthèse en échec D-5, livraison volontairement skippée)
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    delivery_errors: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    signals: Mapped[list["Signal"]] = relationship(back_populates="brief", cascade="all, delete-orphan")


class Signal(Base):
    """Une recommandation/signal extrait d'un brief (pour backtest futur)."""
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("briefs.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # "buy" | "watch" | "avoid" | "hold"
    conviction: Mapped[int] = mapped_column(Integer, default=3)  # 1-5
    thesis: Mapped[str] = mapped_column(Text)
    price_at_signal: Mapped[float | None] = mapped_column(Float)
    signal_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    brief: Mapped["Brief"] = relationship(back_populates="signals")


class ScheduleConfig(Base):
    """Config du scheduler (modifiable via API)."""
    __tablename__ = "schedule_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    cron_expression: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PipelineRun(Base):
    """Audit-trail d'une exécution de pipeline (manuelle ou cron).

    Remplit trois rôles :
    - Debug : savoir pourquoi un brief n'est pas sorti
    - Alerting : flag `status="failed"` pour les watchers
    - Observabilité : durée, étapes, erreurs
    """
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "running" | "success" | "failed" | "skipped_locked"
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="cron")  # "cron" | "manual"
    brief_id: Mapped[int | None] = mapped_column(ForeignKey("briefs.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class Recipient(Base):
    """Destinataire d'un brief (email ou WhatsApp).

    Géré via l'API admin (`/api/recipients`) pour pouvoir ajouter/supprimer
    des destinataires sans redéployer. Un même destinataire peut être désactivé
    temporairement via `enabled=false` plutôt que supprimé (garde l'historique).
    """
    __tablename__ = "recipients"
    __table_args__ = (
        Index("ix_recipients_channel_address", "channel", "address", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)  # "email" | "whatsapp"
    address: Mapped[str] = mapped_column(String(255))  # email ou numéro E.164
    name: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class MarketAnalysis(Base):
    """Analyse quotidienne du marché BRVM générée par Sonnet.

    1 ligne par date. Cache-first : on regarde la DB avant d'appeler Sonnet.
    Régénérable manuellement via l'endpoint dédié.
    """
    __tablename__ = "market_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    trading_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, index=True,
    )
    narrative_fr: Mapped[str] = mapped_column(Text)
    key_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    model_used: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class User(Base):
    """Utilisateur de la console d'administration (auth via magic link email).

    Whitelist simple : seuls les emails présents ici peuvent demander un
    magic link. Le 1er user est seedé depuis INITIAL_ADMIN_EMAIL.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LoginToken(Base):
    """Jeton de magic link — usage unique, TTL court (~15 min).

    Le hash SHA-256 du jeton est stocké (pas le jeton en clair) pour qu'un
    dump DB ne permette pas de s'authentifier.
    """
    __tablename__ = "login_tokens"
    __table_args__ = (
        Index("ix_login_tokens_hash", "token_hash", unique=True),
        Index("ix_login_tokens_email_created", "email", "created_at"),
        Index("ix_login_tokens_ip_created", "requested_ip", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 hex = 64 chars
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))
    requested_ua: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True,
    )
