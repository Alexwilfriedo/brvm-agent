"""Collector pour brvm.org — cotations principales."""
import logging
import re
from datetime import UTC, datetime

import requests
from bs4 import BeautifulSoup

from .base import CollectionResult, Collector, QuoteItem

logger = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; BRVMAgent/1.0)"


class BrvmOfficialCollector(Collector):
    """Récupère les cotations depuis brvm.org.

    Note: la structure HTML peut évoluer. On tolère les erreurs et on
    stocke ce qu'on peut extraire.
    """
    source_key = "brvm_official"
    type = "scraper"

    def collect(self) -> CollectionResult:
        result = CollectionResult(source_key=self.source_key)
        url = self.config.get("url", "https://www.brvm.org/fr/cours-actions/0")
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            resp.raise_for_status()
            result.quotes = self._parse_quotes(resp.text)
            logger.info(f"[{self.source_key}] {len(result.quotes)} cotations")
        except Exception as e:
            msg = f"Échec collecte {url}: {e}"
            logger.error(f"[{self.source_key}] {msg}")
            result.errors.append(msg)
        return result

    def _parse_quotes(self, html: str) -> list[QuoteItem]:
        """Trouve le plus gros tableau de la page et extrait les lignes ressemblant à des cotations."""
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []

        # Le tableau des cours est typiquement le plus grand
        best_table = max(tables, key=lambda t: len(t.find_all("tr")))
        quotes: list[QuoteItem] = []
        now = datetime.now(UTC)

        for row in best_table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 4:
                continue
            ticker = cells[0]
            # Heuristique : ticker BRVM = 3-6 lettres majuscules
            if not re.match(r"^[A-Z]{2,6}$", ticker):
                continue
            try:
                quotes.append(QuoteItem(
                    ticker=ticker,
                    name=cells[1] if len(cells) > 1 else "",
                    close_price=self._num(cells[2]) if len(cells) > 2 else 0.0,
                    variation_pct=self._num(cells[3]) if len(cells) > 3 else 0.0,
                    volume=int(self._num(cells[4])) if len(cells) > 4 else 0,
                    quote_date=now,
                ))
            except Exception as e:
                logger.debug(f"Skip row {cells}: {e}")
        return quotes

    @staticmethod
    def _num(s: str) -> float:
        """Nettoie un nombre au format français (1 234,56 ou 1.234,56 → 1234.56)."""
        if not s:
            return 0.0
        cleaned = s.replace("\xa0", "").replace(" ", "").replace("%", "").replace("+", "")
        # Format français : virgule = décimale
        if "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
