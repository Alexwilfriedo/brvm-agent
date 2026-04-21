"""Collector des communiqués officiels BRVM publiés sur Sika Finance.

Source : https://www.sikafinance.com/marches/communiques_brvm
Chaque entrée de la table HTML = 1 communiqué d'émetteur coté (rapport
trimestriel, état financier, avis de convocation AG, annonce de dividende,
décision de Conseil…) accessible en PDF direct.

Pipeline :
  1. GET HTML listing, parse table (date, titre+ticker, URL PDF)
  2. Filtre par `lookback_hours` pour ne pas retélécharger l'historique
     complet à chaque run (v1 : pas de backfill)
  3. Pour chaque entrée : download PDF + extract texte via `pdf_extractor`
  4. Détection ticker depuis le titre (liste `BRVM_TICKERS` + synonymes)
  5. Émet un `NewsItem` par communiqué — le pipeline standard (persist →
     enrich Sonnet → synthesis Opus) prend le relais sans modification.

Tolérance totale aux erreurs individuelles : un PDF qui ne télécharge pas ne
bloque pas les autres. Les erreurs globales (HTML cassé) remontent dans
`result.errors`.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..database import get_session
from ..models import NewsArticle
from .base import CollectionResult, Collector, NewsItem
from .pdf_extractor import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_SIZE_MB,
    DEFAULT_TIMEOUT_S,
    PdfExtractionError,
    fetch_and_extract,
)
from .sika_quotes import BRVM_TICKERS

logger = logging.getLogger(__name__)

TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)
BASE_URL = "https://www.sikafinance.com"
ABIDJAN_TZ = ZoneInfo("Africa/Abidjan")

# Synonymes connus : nom commun → ticker BRVM. Complète la détection sur
# titres où Sika écrit le nom long plutôt que le ticker (« BICICI » au lieu
# de « BICC », etc.). Maintenu à la main, à compléter au fil de l'usage.
_NAME_SYNONYMS: dict[str, str] = {
    "bicici": "BICC",
    "sgbci": "SGBC",
    "sicable": "CABC",
    "sicor": "SICC",
    "palmci": "PALC",
    "solibra": "SLBC",
    "nestlé ci": "NTLC",
    "nestle ci": "NTLC",
    "total ci": "TTLC",
    "totalenergies ci": "TTLC",
    "totalenergies marketing côte d'ivoire": "TTLC",
    "onatel": "ONTBF",
    "sonatel": "SNTS",
    "servair": "ABJC",
    "air liquide": "SIVC",
}

# Regex ticker BRVM : 2-6 lettres majuscules, entouré de non-alphanum ou bord.
_TICKER_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2,6})(?![A-Z0-9])")


def _normalize(text: str) -> str:
    """Lower + strip accents pour matcher les synonymes quelle que soit la casse."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


class SikaCommuniquesCollector(Collector):
    """Scrape la page des communiqués BRVM + extrait le texte des PDFs.

    Logique de mémoire : la page Sika liste environ 30 communiqués sur ~20 jours.
    À chaque run on les parcourt TOUS, on skip ceux dont l'URL PDF est déjà en
    DB (table news), et on ne télécharge/enrichit que les nouveaux. Ça garantit
    que si un run rate un lundi matin ou si l'instance est down pendant une
    semaine, on rattrape automatiquement au prochain run sans intervention.

    `lookback_hours` n'est PLUS un filtre métier — c'est un cap de sécurité
    (défaut 30 jours) au cas où Sika retournerait un jour de l'historique
    profond. Le vrai filtre est la dedup par URL.

    Config attendue (tous optionnels, avec défauts) :
        {
          "url": "https://www.sikafinance.com/marches/communiques_brvm",
          "lookback_hours": 720,      # cap sécurité (30 jours)
          "max_items_per_run": 20,    # cap défensif — protège coût Sonnet
          "pdf_max_chars": 15000,     # troncature texte extrait
          "pdf_max_size_mb": 10,      # abort téléchargement si plus gros
          "pdf_timeout_s": 20,        # timeout HTTP par PDF
        }
    """

    source_key = "sika_communiques"
    type = "sika_communiques"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._known_tickers: set[str] = {t.ticker for t in BRVM_TICKERS}

    def collect(self, run_id: int | None = None) -> CollectionResult:
        result = CollectionResult(source_key=self.source_key)
        url = self.config.get(
            "url", f"{BASE_URL}/marches/communiques_brvm",
        )
        # Cap de sécurité large (30 jours par défaut). La vraie dedup se fait
        # par URL en DB juste en dessous.
        lookback_hours = int(self.config.get("lookback_hours", 720))
        max_items = int(self.config.get("max_items_per_run", 20))
        pdf_max_chars = int(self.config.get("pdf_max_chars", DEFAULT_MAX_CHARS))
        pdf_max_size_mb = int(self.config.get("pdf_max_size_mb", DEFAULT_MAX_SIZE_MB))
        pdf_timeout_s = int(self.config.get("pdf_timeout_s", DEFAULT_TIMEOUT_S))

        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            entries = self._parse_listing(resp.text, url)
        except Exception as e:
            msg = f"Échec récupération listing {url} : {e}"
            logger.error(f"[{self.source_key}] {msg}")
            result.errors.append(msg)
            return result

        if not entries:
            # Pas d'entrées → probable changement HTML. On remonte en WARNING
            # dans les errors pour que ce soit visible dans le dashboard runs.
            msg = f"Aucune entrée parsée dans {url} — HTML a peut-être changé ?"
            logger.warning(f"[{self.source_key}] {msg}")
            result.errors.append(msg)
            return result

        # Cap de sécurité par âge (protection contre un listing qui retournerait
        # un jour 10 ans d'historique). Normalement inutile vu que la page Sika
        # ne montre que ~20 jours.
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        candidates = [(d, t, u) for (d, t, u) in entries if d >= cutoff]

        # Dedup par URL déjà ingérée : on ne télécharge PAS ce qu'on a déjà vu,
        # ce qui est critique pour les PDFs (coût network + Sonnet). C'est aussi
        # ce qui permet de "rattraper la mémoire" après une panne — au prochain
        # run, on ingère tout ce qui est visible et pas encore en DB.
        known_urls = self._load_known_urls()
        fresh = [(d, t, u) for (d, t, u) in candidates if u not in known_urls]
        logger.info(
            f"[{self.source_key}] {len(entries)} entrées listées · "
            f"{len(candidates)} dans le cap {lookback_hours}h · "
            f"{len(known_urls)} déjà connues en DB · "
            f"{len(fresh)} à traiter (cap {max_items})."
        )
        fresh = fresh[:max_items]

        # Téléchargement + extraction séquentiels (PDF coûteux, éviter de
        # marteler Sika en parallèle — 20 PDFs à ~1s chacun = 20s, tolérable).
        for published_at, title, pdf_url in fresh:
            try:
                content = fetch_and_extract(
                    pdf_url,
                    timeout_s=pdf_timeout_s,
                    max_size_mb=pdf_max_size_mb,
                    max_chars=pdf_max_chars,
                )
            except PdfExtractionError as e:
                msg = f"Skip {pdf_url} : {e}"
                logger.warning(f"[{self.source_key}] {msg}")
                result.errors.append(msg)
                continue

            if not content:
                # Image-only / chiffré → pas utilisable pour enrichment Sonnet
                logger.info(
                    f"[{self.source_key}] Skip {pdf_url} : texte vide "
                    f"(PDF image-only ou chiffré)"
                )
                continue

            tickers = self._extract_tickers(title)
            result.news.append(NewsItem(
                source_key=self.source_key,
                title=title[:500],
                url=pdf_url,
                published_at=published_at,
                summary=title[:300],
                content=content,
                tickers_mentioned=tickers,
            ))

        logger.info(
            f"[{self.source_key}] {len(result.news)} communiqué(s) capturé(s) "
            f"avec texte PDF exploité, {len(result.errors)} erreur(s)."
        )
        return result

    def _load_known_urls(self) -> set[str]:
        """Charge l'ensemble des URLs PDF déjà en DB pour ce source_key.

        Sert de mémoire persistante : évite de re-télécharger/re-enrichir un
        communiqué déjà ingéré, tout en laissant le collector parcourir la
        page complète à chaque run (rattrapage automatique après panne).

        1 requête par run, filtrée par source_key, donc bornée (~30 rows par
        mois de fonctionnement). Négligeable.
        """
        try:
            with get_session() as s:
                rows = s.execute(
                    select(NewsArticle.url).where(
                        NewsArticle.source_key == self.source_key,
                    )
                ).scalars().all()
                return {url for url in rows if url}
        except Exception as e:
            # Si la DB est muette, on préfère re-télécharger que faire échouer
            # le run. Le pipeline dédupliquera de toute façon à la persistance.
            logger.warning(
                f"[{self.source_key}] Impossible de charger les URLs connues "
                f"({e}) — fallback : pas de dedup côté collector."
            )
            return set()

    def _parse_listing(
        self, html: str, base_url: str,
    ) -> list[tuple[datetime, str, str]]:
        """Extrait (published_at UTC, title, pdf_absolute_url) de la table HTML.

        Heuristique robuste aux changements mineurs de structure : on cherche
        le plus grand `<table>` de la page, puis on valide chaque ligne par
        la présence d'une date au format DD/MM/YYYY + d'un lien `.pdf`.
        """
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []
        # Typiquement le tableau communiqués est le plus grand
        best_table = max(tables, key=lambda t: len(t.find_all("tr")))

        out: list[tuple[datetime, str, str]] = []
        for row in best_table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            # Chercher la date (DD/MM/YYYY) dans n'importe quelle cellule
            published_at = None
            for c in cells:
                txt = c.get_text(strip=True)
                m = re.match(r"(\d{2})/(\d{2})/(\d{4})", txt)
                if m:
                    dd, mm, yyyy = map(int, m.groups())
                    try:
                        local = datetime(
                            yyyy, mm, dd, tzinfo=ABIDJAN_TZ,
                        )
                        published_at = local.astimezone(UTC)
                        break
                    except ValueError:
                        continue
            if not published_at:
                continue

            # Premier lien PDF dans la ligne
            link = None
            for a in row.find_all("a", href=True):
                href = a["href"].strip()
                if href.lower().endswith(".pdf"):
                    link = urljoin(base_url, href)
                    break
            if not link:
                continue

            # Titre : concat du texte de toutes les cellules moins la date,
            # puis strip + collapse whitespace. Plus robuste que "prend la
            # Nième cellule" face à des changements de layout.
            all_text = " ".join(c.get_text(" ", strip=True) for c in cells)
            title = re.sub(r"\s+", " ", all_text).strip()
            # Retire la date du titre (cosmétique)
            title = re.sub(r"\d{2}/\d{2}/\d{4}", "", title, count=1).strip()
            if not title:
                continue

            out.append((published_at, title, link))

        return out

    def _extract_tickers(self, title: str) -> list[str]:
        """Best-effort : retourne la liste de tickers BRVM détectés dans le titre.

        Sonnet fera son propre pass d'enrichment ensuite — cette détection
        n'est qu'un booster pour les cas évidents.
        """
        found: set[str] = set()

        # 1. Match direct sur tickers en majuscules dans le titre
        for m in _TICKER_RE.finditer(title):
            candidate = m.group(1)
            if candidate in self._known_tickers:
                found.add(candidate)

        # 2. Match par synonymes de noms longs (avec frontière de mot pour
        # éviter "onatel" matchant dans "sonatel").
        norm = _normalize(title)
        for alias, ticker in _NAME_SYNONYMS.items():
            pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
            if re.search(pattern, norm) and ticker in self._known_tickers:
                found.add(ticker)

        return sorted(found)
