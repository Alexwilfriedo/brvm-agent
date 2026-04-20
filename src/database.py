"""Gestion de la base PostgreSQL.

Migrations : on reste en `create_all()` + migrations inline idempotentes
(`_INLINE_MIGRATIONS`) tant que le schéma bouge peu. À basculer sur Alembic
dès que les altérations deviennent non-trivales (renommage, typage…).
"""
import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings = get_settings()

# Railway fournit DATABASE_URL qui commence parfois par "postgres://" (ancien format).
# SQLAlchemy 2 veut "postgresql://".
db_url = settings.database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    db_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=10,  # fail-fast plutôt que l'accumulation de requêtes en attente
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@contextmanager
def get_session() -> Session:
    """Session context manager — commit/rollback automatique."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Migrations inline ------------------------------------------------------

# Liste ordonnée d'ALTER TABLE idempotents. Chaque entrée utilise
# `IF NOT EXISTS` pour pouvoir tourner à chaque boot sans casser. On évite
# Alembic tant que les altérations restent compatibles (ajout de colonnes
# nullable, extensions). Toute modif destructive (drop/rename) → Alembic.
_INLINE_MIGRATIONS: list[str] = [
    # 2026-04 : métriques détaillées Sika (open/high/low/RSI/...) persistées en `extras`,
    # et `country` pour l'URL source du collector.
    "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS country VARCHAR(8)",
    "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS extras JSON DEFAULT '{}'::json",
    # 2026-04 (pattern C) : briefs idempotents par date + révisions versionnées.
    "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS revised_at TIMESTAMP WITH TIME ZONE",
    # Note : l'unicité par date calendaire est enforçée **côté application**
    # via `_find_brief_for_date` dans pipeline.py. Un index fonctionnel
    # Postgres type `((brief_date::date))` nécessiterait que le cast soit
    # IMMUTABLE (il est STABLE à cause du paramètre TimeZone), et on ne peut
    # pas l'ajouter rétroactivement tant que la table contient des doublons
    # historiques (avant pattern C). À migrer plus tard avec Alembic quand
    # on aura dédupliqué l'historique.
]


def _run_inline_migrations() -> None:
    """Applique les ALTER idempotents. Silencieux si la DB n'est pas Postgres."""
    if not engine.dialect.name.startswith("postgres"):
        return
    with engine.begin() as conn:
        for sql in _INLINE_MIGRATIONS:
            try:
                conn.execute(text(sql))
            except Exception as e:  # noqa: BLE001
                # Ne pas planter tout le boot si une migration échoue — log + skip.
                # Le contrat `IF NOT EXISTS` devrait rendre ça rare.
                logger.warning(f"Migration inline ignorée : {sql[:60]}… ({e})")


def init_db() -> None:
    """Crée les tables manquantes + applique les migrations inline."""
    # Import tardif pour éviter circular imports
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_inline_migrations()
