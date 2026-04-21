"""Registry des collectors : permet d'ajouter/activer/désactiver les sources
dynamiquement depuis la DB (préparé pour l'UI future)."""
import logging

from .base import Collector
from .brvm_official import BrvmOfficialCollector
from .sika_finance import RssCollector, SikaFinanceCollector
from .sika_quotes import SikaQuotesCollector

logger = logging.getLogger(__name__)


# Mapping type → classe. Ajouter ici chaque nouveau collector.
COLLECTOR_CLASSES: dict[str, type[Collector]] = {
    "brvm_official": BrvmOfficialCollector,     # legacy — cassé (brvm.org HTML change)
    "sika_finance": SikaFinanceCollector,        # legacy — flux RSS illisible
    "sika_quotes": SikaQuotesCollector,          # cotations détaillées par ticker
    "rss": RssCollector,                         # RSS générique — URL vient de la DB
}


def build_collector(source_type: str, config: dict) -> Collector | None:
    """Instancie un collector depuis son type + config DB."""
    cls = COLLECTOR_CLASSES.get(source_type)
    if not cls:
        logger.warning(f"Type collector inconnu : {source_type}")
        return None
    return cls(config=config)


# Sources par défaut (seedées en DB au premier démarrage).
# Uniquement les sources confirmées fonctionnelles en prod — les legacy cassées
# (brvm_official, sika_finance RSS) ne sont PAS seedées. Elles restent en DB
# sur les anciens déploiements (désactivées) et ne gênent pas.
DEFAULT_SOURCES = [
    {
        "key": "sika_finance_quotes",
        "name": "Sika Finance - Cotations détaillées par ticker",
        "type": "sika_quotes",
        "url": "https://www.sikafinance.com/marches/cotation_{ticker}.{country}",
        "config": {"max_workers": 6},
    },
    {
        "key": "financial_afrik",
        "name": "Financial Afrik — Actualités UEMOA/BRVM",
        "type": "rss",
        "url": "https://www.financialafrik.com/feed/",
        "config": {"lookback_hours": 48},
    },
    {
        "key": "jeune_afrique",
        "name": "Jeune Afrique — Économie (panafricain)",
        "type": "rss",
        "url": "https://www.jeuneafrique.com/feed/",
        "config": {"lookback_hours": 48},
    },
    {
        "key": "lefaso",
        "name": "LeFaso.net — Actualités Burkina Faso",
        "type": "rss",
        "url": "https://lefaso.net/spip.php?page=backend",
        "config": {"lookback_hours": 48},
    },
]
