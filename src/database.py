"""Gestion de la base PostgreSQL."""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

# Railway fournit DATABASE_URL qui commence parfois par "postgres://" (ancien format).
# SQLAlchemy 2 veut "postgresql://".
db_url = settings.database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
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


def init_db() -> None:
    """Crée les tables si elles n'existent pas (MVP ; passer à Alembic quand le schéma évolue)."""
    # Import tardif pour éviter circular imports
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
