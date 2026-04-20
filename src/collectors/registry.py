"""Registry des collectors : permet d'ajouter/activer/désactiver les sources
dynamiquement depuis la DB (préparé pour l'UI future)."""
import logging

from .base import Collector
from .brvm_official import BrvmOfficialCollector
from .sika_finance import RssCollector, SikaFinanceCollector

logger = logging.getLogger(__name__)


# Mapping type → classe. Ajouter ici chaque nouveau collector.
COLLECTOR_CLASSES: dict[str, type[Collector]] = {
    "brvm_official": BrvmOfficialCollector,
    "sika_finance": SikaFinanceCollector,
    "rss": RssCollector,  # RSS générique — l'URL vient de la DB
}


def build_collector(source_type: str, config: dict) -> Collector | None:
    """Instancie un collector depuis son type + config DB."""
    cls = COLLECTOR_CLASSES.get(source_type)
    if not cls:
        logger.warning(f"Type collector inconnu : {source_type}")
        return None
    return cls(config=config)


# Sources par défaut (seedées en DB au premier démarrage)
DEFAULT_SOURCES = [
    {
        "key": "brvm_official",
        "name": "BRVM - Cotations officielles",
        "type": "brvm_official",
        "url": "https://www.brvm.org/fr/cours-actions/0",
        "config": {},
    },
    {
        "key": "sika_finance",
        "name": "Sika Finance - Actualités",
        "type": "sika_finance",
        "url": "https://www.sikafinance.com/rss/actualites_11.xml",
        "config": {"lookback_hours": 36},
    },
]
