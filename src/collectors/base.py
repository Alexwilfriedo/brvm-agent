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
    ticker: str
    name: str
    sector: str = ""
    close_price: float = 0.0
    variation_pct: float = 0.0
    volume: int = 0
    value_traded: float = 0.0
    quote_date: datetime | None = None


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
    def collect(self) -> CollectionResult:
        """Récupère les données. Ne lève PAS d'exception — les erreurs
        sont retournées dans CollectionResult.errors."""
        raise NotImplementedError
