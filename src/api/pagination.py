"""Helpers pour pagination + recherche fuzzy (pg_trgm).

Contrat API unifié :
    GET /api/<resource>?q=...&limit=50&offset=0

Réponse :
    {
      "items": [...],
      "total": 123,
      "limit": 50,
      "offset": 0
    }

La recherche (`q`) utilise ILIKE côté SQL — les index trigrammes GIN
(cf. `database.py`) accélèrent transparentement les requêtes sur les
colonnes indexées.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.selectable import Select

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Enveloppe standard des endpoints list."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[T]
    total: int
    limit: int = Field(ge=0)
    offset: int = Field(ge=0)


# --- Limites pour éviter les abus ------------------------------------------

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


# --- Search helpers ---------------------------------------------------------

def _escape_like(value: str) -> str:
    """Échappe les wildcards SQL dans un terme de recherche utilisateur.

    Évite qu'un `%` dans `q` ne match tout, ou qu'un `_` ne matche n'importe
    quel caractère. On utilise `\\` comme caractère d'échappement.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def ilike_any(columns: list[InstrumentedAttribute], q: str) -> ColumnElement[bool]:
    """Construit un OR de `col ILIKE %q%` sur plusieurs colonnes.

    Usage :
        stmt = select(Brief)
        if q:
            stmt = stmt.where(ilike_any([Brief.summary_markdown], q))
    """
    pattern = f"%{_escape_like(q)}%"
    clauses = [col.ilike(pattern, escape="\\") for col in columns]
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


# --- Application helper -----------------------------------------------------

def paginate(
    session: Session,
    stmt: Select,
    *,
    limit: int,
    offset: int,
) -> tuple[list, int]:
    """Exécute une requête paginée.

    Renvoie `(items, total)`. Le `total` est calculé via un COUNT(*)
    sur la même requête sans le `LIMIT/OFFSET`, ce qui reste léger
    sur nos tables (< 10k lignes).
    """
    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    items = session.execute(
        stmt.limit(clamp_limit(limit)).offset(max(0, offset))
    ).scalars().all()
    return items, total
