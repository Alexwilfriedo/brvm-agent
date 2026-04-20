"""Collector RSS générique — utilisable pour Sika Finance, Financial Afrik, etc."""
import logging
from datetime import UTC, datetime, timedelta

import feedparser

from .base import CollectionResult, Collector, NewsItem

logger = logging.getLogger(__name__)


class RssCollector(Collector):
    """Collector RSS générique. Config attendue :
        {
          "url": "https://www.sikafinance.com/rss/actualites_11.xml",
          "lookback_hours": 36
        }
    """
    source_key = "rss_generic"
    type = "rss"

    def collect(self) -> CollectionResult:
        result = CollectionResult(source_key=self.source_key)
        url = self.config.get("url")
        if not url:
            result.errors.append("Config RSS sans 'url'")
            return result
        lookback = self.config.get("lookback_hours", 36)
        threshold = datetime.now(UTC) - timedelta(hours=lookback)

        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                result.errors.append(f"Flux RSS illisible : {feed.bozo_exception}")
                return result

            for entry in feed.entries:
                published_at = self._parse_date(entry)
                if published_at and published_at < threshold:
                    continue
                result.news.append(NewsItem(
                    source_key=self.source_key,
                    title=getattr(entry, "title", "")[:500],
                    url=getattr(entry, "link", ""),
                    published_at=published_at,
                    summary=self._clean(getattr(entry, "summary", "")),
                ))
            logger.info(f"[{self.source_key}] {len(result.news)} articles récents depuis {url}")
        except Exception as e:
            result.errors.append(f"Échec RSS {url}: {e}")
            logger.error(f"[{self.source_key}] {e}")
        return result

    @staticmethod
    def _parse_date(entry) -> datetime | None:
        for field_name in ("published_parsed", "updated_parsed"):
            val = getattr(entry, field_name, None)
            if val:
                try:
                    return datetime(*val[:6], tzinfo=UTC)
                except Exception:
                    pass
        return None

    @staticmethod
    def _clean(text: str, max_len: int = 1000) -> str:
        """Nettoie le HTML basique d'un résumé RSS."""
        from bs4 import BeautifulSoup
        if not text:
            return ""
        clean = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        return clean[:max_len]


class SikaFinanceCollector(RssCollector):
    source_key = "sika_finance"

    def __init__(self, config: dict | None = None):
        cfg = {"url": "https://www.sikafinance.com/rss/actualites_11.xml", "lookback_hours": 36}
        cfg.update(config or {})
        super().__init__(cfg)
