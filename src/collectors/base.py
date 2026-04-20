"""Interface commune pour tous les collecteurs de données."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    source_key: str
    title: str
    url: str
    published_at: datetime | None = None
    summary: str = ""
    content: str = ""
    tickers_mentioned: list[str] = field(default_factory=list)


@dataclass
class QuoteItem:
    """Cotation remontée par un collecteur.

    `country` : code pays UEMOA (ci/sn/tg/bj/ml/ne/bf), utile pour l'URL source.
    `extras`  : métriques secondaires (open/high/low/prev, RSI, beta, PER,
                dividende, capi…) — schéma libre selon le collecteur.
    """
    ticker: str
    name: str
    sector: str = ""
    country: str | None = None
    close_price: float = 0.0
    variation_pct: float = 0.0
    volume: int = 0
    value_traded: float = 0.0
    quote_date: datetime | None = None
    extras: dict = field(default_factory=dict)


@dataclass
class CollectionResult:
    source_key: str
    news: list[NewsItem] = field(default_factory=list)
    quotes: list[QuoteItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class Collector(ABC):
    """Interface que tout collecteur doit implémenter."""

    source_key: str = "base"
    type: str = "generic"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    def collect(self, run_id: int | None = None) -> CollectionResult:
        """Récupère les données. Ne lève PAS d'exception — les erreurs
        sont retournées dans CollectionResult.errors.

        `run_id` permet aux collecteurs d'émettre des events SSE de
        progression. Optionnel : les collecteurs qui n'en ont pas besoin
        l'ignorent.
        """
        raise NotImplementedError
