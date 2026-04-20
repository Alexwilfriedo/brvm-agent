"""Collector Sika Finance — cotations détaillées par ticker.

Scrape `https://www.sikafinance.com/marches/cotation_<TICKER>.<ext>` pour
chaque entreprise cotée BRVM et extrait toutes les métriques disponibles :

    - cours, variation %, volume (titres/devises), capital échangé
    - ouverture, plus haut, plus bas, clôture veille
    - valorisation, dividende, rendement
    - beta 1 an, RSI

Le référentiel des 45 entreprises est maintenu en dur dans `BRVM_TICKERS`.
Il bouge rarement (admissions/radiations sont des événements) — quand ça
arrive, on met la liste à jour ici et on redéploie.

Design :
  - Scraping **parallèle** via `ThreadPoolExecutor` (default 6 workers) pour
    passer de ~20s séquentiel à ~3-4s. `requests.Session` est thread-safe
    pour des requêtes indépendantes.
  - Tolérance totale aux erreurs individuelles : 1 ticker qui échoue ne
    bloque pas les 44 autres.
  - Config tunable : `max_workers` pour plus/moins d'agressivité.
"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import NamedTuple

import certifi
import requests
from bs4 import BeautifulSoup

from .. import events
from .base import CollectionResult, Collector, QuoteItem

logger = logging.getLogger(__name__)

TIMEOUT = 15
DEFAULT_MAX_WORKERS = 6  # 45 requêtes / 6 workers = 8 vagues séquentielles
BASE_URL = "https://www.sikafinance.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)


class Listed(NamedTuple):
    ticker: str
    country: str  # "ci" | "sn" | "tg" | "bj" | "ml" | "ne" | "bf"
    name: str
    sector: str   # "Banque", "Télécoms", "Agriculture", "Industrie", "Distribution", ...


# Liste maintenue en dur — 48 entreprises cotées BRVM (avril 2026).
# Source : https://www.sikafinance.com/marches/aaz
BRVM_TICKERS: list[Listed] = [
    Listed("SDSC", "ci", "Africa Global Logistics", "Transport"),
    Listed("BOAB", "bj", "Bank of Africa Bénin", "Banque"),
    Listed("BOABF", "bf", "Bank of Africa Burkina Faso", "Banque"),
    Listed("BOAC", "ci", "Bank of Africa Côte d'Ivoire", "Banque"),
    Listed("BOAM", "ml", "Bank of Africa Mali", "Banque"),
    Listed("BOAN", "ne", "Bank of Africa Niger", "Banque"),
    Listed("BOAS", "sn", "Bank of Africa Sénégal", "Banque"),
    Listed("BICB", "bj", "Banque Internationale pour le Commerce du Bénin", "Banque"),
    Listed("BNBC", "ci", "Bernabé", "Distribution"),
    Listed("BICC", "ci", "BICICI", "Banque"),
    Listed("CFAC", "ci", "CFAO Motors CI", "Distribution"),
    Listed("CIEC", "ci", "CIE CI", "Services publics"),
    Listed("CBIBF", "bf", "Coris Bank International BF", "Banque"),
    Listed("SEMC", "ci", "Crown Siem", "Industrie"),
    Listed("ECOC", "ci", "Ecobank CI", "Banque"),
    Listed("SIVC", "ci", "Erium", "Industrie"),
    Listed("ETIT", "tg", "ETI Togo", "Banque"),
    Listed("FTSC", "ci", "Filtisac CI", "Industrie"),
    Listed("LNBB", "bj", "Loterie Nationale du Bénin", "Services"),
    Listed("SVOC", "ci", "Movis CI", "Industrie"),
    Listed("NEIC", "ci", "NEI CEDA CI", "Services"),
    Listed("NTLC", "ci", "Nestlé CI", "Consommation"),
    Listed("NSBC", "ci", "NSIA Banque", "Banque"),
    Listed("ONTBF", "bf", "Onatel BF", "Télécoms"),
    Listed("ORGT", "tg", "Oragroup Togo", "Banque"),
    Listed("ORAC", "ci", "Orange CI", "Télécoms"),
    Listed("PALC", "ci", "PALMCI", "Agriculture"),
    Listed("SAFC", "ci", "Safca CI", "Services financiers"),
    Listed("SPHC", "ci", "SAPH CI", "Agriculture"),
    Listed("ABJC", "ci", "Servair Abidjan CI", "Services"),
    Listed("STAC", "ci", "Setao CI", "Industrie"),
    Listed("SGBC", "ci", "SGBCI", "Banque"),
    Listed("CABC", "ci", "Sicable CI", "Industrie"),
    Listed("SICC", "ci", "SICOR", "Agriculture"),
    Listed("STBC", "ci", "SITAB", "Consommation"),
    Listed("SMBC", "ci", "SMB CI", "Industrie"),
    Listed("SIBC", "ci", "Société Ivoirienne de Banque", "Banque"),
    Listed("SDCC", "ci", "SODECI", "Services publics"),
    Listed("SOGC", "ci", "SOGB", "Agriculture"),
    Listed("SLBC", "ci", "Solibra CI", "Consommation"),
    Listed("SNTS", "sn", "Sonatel", "Télécoms"),
    Listed("SCRC", "ci", "Sucrivoire", "Agriculture"),
    Listed("TTLC", "ci", "Total CI", "Distribution"),
    Listed("TTLS", "sn", "Total Sénégal", "Distribution"),
    Listed("PRSC", "ci", "Tractafric Motors CI", "Distribution"),
    Listed("UNLC", "ci", "Unilever CI", "Consommation"),
    Listed("UNXC", "ci", "Uniwax CI", "Industrie"),
    Listed("SHEC", "ci", "Vivo Energy CI", "Distribution"),
]


# Labels FR → clé normalisée dans `extras`
_LABEL_MAP: dict[str, str] = {
    "cours": "close_price",
    "ouverture": "open_price",
    "plus haut": "high_price",
    "plus bas": "low_price",
    "clôture veille": "previous_close",
    "cloture veille": "previous_close",
    "volume (titres)": "volume_shares",
    "volume (devises)": "volume_value_fcfa",
    "capital échangé": "capital_traded_pct",
    "capital echange": "capital_traded_pct",
    "valorisation": "market_cap_mfcfa",
    "beta 1 an": "beta_1y",
    "beta": "beta_1y",
    "rsi": "rsi",
    "dividende": "dividend",
    "dividendes": "dividend",
    "rendement": "dividend_yield_pct",
    "per": "per",
    "pe": "per",
}


def _num_fr(s: str) -> float | None:
    """Convertit un nombre au format français ('1 775', '1 234,56', '-0,56%')."""
    if s is None:
        return None
    cleaned = (
        str(s)
        .replace("\xa0", "")
        .replace(" ", "")
        .replace("%", "")
        .replace("+", "")
    )
    if not cleaned or cleaned in {"-", "—", "nd", "n/d", "na", "n/a"}:
        return None
    # Format français : virgule = décimale
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


_NUMBER_RE = re.compile(r"^-?[\d\s\u00a0.,]+$")
_PERCENT_RE = re.compile(r"^[+\-]?[\d\s\u00a0.,]+%$")


def _parse_ticker_page(html: str) -> dict:
    """Parse une page `/marches/cotation_<TICKER>.<ext>`.

    La page suit ce layout (observé sur BOAC, SDSC, etc.) :

        BANK OF AFRICA CI
        CI0000000956 - BOAC
        La BRVM Ouvre dans 14h54min
        COURS            <-- en-tête
        GRAPHIQUES       ┐
        ACTUS            │
        ANALYSE           │ onglets de navigation
        HISTORIQUES      │
        SECTEUR          │
        EVENEMENTS       │
        FORUM            │
        SOCIETE          ┘
        8 695            <-- COURS actuel (juste après le dernier tab)
        +1,10%           <-- variation
        Volume (titres)
        58 194
        Volume ( )       <-- bug Sika, label vide
        505 996 830
        Ouverture
        8 600
        Plus haut
        8 695
        ...

    Stratégie :
      1. On localise "COURS" en en-tête, on saute les onglets de nav,
         on récupère la 1ʳᵉ valeur numérique (close) puis la 2ᵉ (% variation).
      2. Passage "label → valeur" sur chaque paire successive (ligne N = label,
         ligne N+1 = valeur). Robuste au label vide "Volume ( )".
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    metrics: dict = {}

    # 1. Localiser COURS + variation
    # Tabs connues — on doit les sauter pour atteindre la valeur
    _NAV_TABS = {
        "cours", "graphiques", "actus", "analyse", "analyse et conseils",
        "historiques", "secteur", "evenements", "événements", "forum", "societe", "société",
    }
    try:
        # Index de la 1re occurrence du label "COURS" comme en-tête (et pas dans la nav)
        cours_idx = None
        for i, line in enumerate(lines):
            if line.lower() == "cours":
                cours_idx = i
                break
        if cours_idx is not None:
            # Avance jusqu'à la 1re ligne qui n'est pas un tab de nav
            j = cours_idx + 1
            while j < len(lines) and lines[j].lower() in _NAV_TABS:
                j += 1
            # lines[j] devrait être la valeur du cours (nombre)
            if j < len(lines):
                n = _num_fr(lines[j]) if _NUMBER_RE.match(lines[j]) else None
                if n is not None:
                    metrics["close_price"] = n
                # lines[j+1] devrait être la variation en %
                if j + 1 < len(lines) and _PERCENT_RE.match(lines[j + 1]):
                    var = _num_fr(lines[j + 1])
                    if var is not None:
                        metrics["variation_pct"] = var
    except Exception:
        logger.debug("parse cours en-tête échoué", exc_info=True)

    # 2. Passage label → valeur (ligne N = label, N+1 = valeur)
    for i, line in enumerate(lines):
        label_lower = line.lower().strip()
        # Tolérance aux labels tronqués : "volume ( )" → "volume (devises)"
        if re.match(r"volume\s*\(\s*\)$", label_lower):
            key = "volume_value_fcfa"
        else:
            key = _LABEL_MAP.get(label_lower)
        if not key or key in metrics:
            continue
        if i + 1 >= len(lines):
            continue
        next_line = lines[i + 1]
        # Accept both pure numbers and percents (strip % inside _num_fr)
        n = _num_fr(next_line)
        if n is not None:
            metrics[key] = n

    return metrics


def _scrape_one(
    session: requests.Session,
    listed: Listed,
    now: datetime,
    run_id: int | None = None,
) -> tuple[QuoteItem | None, str | None]:
    """Scrape une seule page ticker. Retourne (QuoteItem|None, erreur|None).

    Fonction pure à passer au ThreadPoolExecutor. Ne lève pas d'exception :
    toute erreur est retournée comme string. Émet des events SSE si `run_id`
    est fourni (visualisation live par thread).
    """
    worker = threading.current_thread().name
    if run_id is not None:
        events.publish(
            run_id, "ticker.start",
            ticker=listed.ticker, country=listed.country,
            name=listed.name, sector=listed.sector, worker=worker,
        )
    url = f"{BASE_URL}/marches/cotation_{listed.ticker}.{listed.country}"
    try:
        resp = session.get(url, timeout=TIMEOUT, verify=certifi.where())
        resp.raise_for_status()
        metrics = _parse_ticker_page(resp.text)
        if not metrics:
            if run_id is not None:
                events.publish(run_id, "ticker.error", ticker=listed.ticker,
                               worker=worker, error="aucune métrique")
            return None, f"{listed.ticker}: aucune métrique extraite"

        close = metrics.pop("close_price", None)
        prev_close = metrics.get("previous_close")
        volume = int(metrics.pop("volume_shares", 0) or 0)
        value_traded = metrics.pop("volume_value_fcfa", 0.0) or 0.0
        # Variation lue directement sur la page si dispo (plus fiable que le calcul)
        parsed_variation = metrics.pop("variation_pct", None)

        # Titre peu liquide / pas coté aujourd'hui : close manquant ou 0 avec volume 0
        # → on prend previous_close comme référence, variation = 0%.
        if (close is None or close == 0) and volume == 0 and prev_close:
            close = prev_close
            variation_pct = 0.0
        elif parsed_variation is not None:
            variation_pct = parsed_variation
        elif close and prev_close and prev_close > 0:
            variation_pct = round((close - prev_close) / prev_close * 100, 4)
        else:
            variation_pct = 0.0
        close = close or 0.0

        quote = QuoteItem(
            ticker=listed.ticker,
            name=listed.name,
            sector=listed.sector,
            country=listed.country,
            quote_date=now,
            close_price=close,
            variation_pct=variation_pct,
            volume=volume,
            value_traded=value_traded,
            extras=metrics,
        )
        if run_id is not None:
            events.publish(
                run_id, "ticker.done",
                ticker=listed.ticker, worker=worker,
                close_price=close, variation_pct=variation_pct,
                volume=volume, sector=listed.sector,
            )
        return quote, None
    except requests.RequestException as e:
        if run_id is not None:
            events.publish(run_id, "ticker.error", ticker=listed.ticker,
                           worker=worker, error=f"HTTP {e}")
        return None, f"{listed.ticker}: HTTP {e}"
    except Exception as e:
        logger.exception(f"[sika_quotes] {listed.ticker} parse error")
        if run_id is not None:
            events.publish(run_id, "ticker.error", ticker=listed.ticker,
                           worker=worker, error=str(e))
        return None, f"{listed.ticker}: {e}"


class SikaQuotesCollector(Collector):
    """Collecte les cotations détaillées de chaque ticker BRVM via Sika Finance.

    Scraping **parallèle** avec pool de threads (I/O bound → le GIL libère
    pendant les requêtes HTTP, donc threads suffisent, pas besoin d'asyncio).

    Config (tous optionnels) :
        {
          "tickers":     ["SNTS", "BOAC", ...],   # filtre, sinon toutes les 45
          "max_workers": 6                        # 1 = séquentiel, 10 = max reco
        }
    """

    source_key = "sika_quotes"
    type = "sika_quotes"

    def collect(self, run_id: int | None = None) -> CollectionResult:
        result = CollectionResult(source_key=self.source_key)
        filter_tickers: list[str] | None = self.config.get("tickers")
        max_workers = max(1, min(int(self.config.get("max_workers", DEFAULT_MAX_WORKERS)), 10))

        tickers = [t for t in BRVM_TICKERS if not filter_tickers or t.ticker in filter_tickers]
        if not tickers:
            result.errors.append("Aucun ticker à collecter (filtre trop restrictif ?).")
            return result

        if run_id is not None:
            events.publish(
                run_id, "source.scrape_start",
                source_key=self.source_key,
                total_tickers=len(tickers),
                max_workers=max_workers,
            )

        now = datetime.now(UTC)
        started = datetime.now(UTC)

        # Session partagée thread-safe (keep-alive + pool interne).
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        # Agrandit le pool HTTPS interne pour matcher le nombre de workers.
        adapter = requests.adapters.HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        logger.info(f"[sika_quotes] Scraping {len(tickers)} ticker(s) avec {max_workers} worker(s)…")

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sika") as pool:
            futures = {
                pool.submit(_scrape_one, session, t, now, run_id): t
                for t in tickers
            }
            for fut in as_completed(futures):
                quote, err = fut.result()
                if quote is not None:
                    result.quotes.append(quote)
                if err is not None:
                    result.errors.append(err)
                    logger.warning(f"[sika_quotes] {err}")

        session.close()

        elapsed = (datetime.now(UTC) - started).total_seconds()
        logger.info(
            f"[sika_quotes] {len(result.quotes)}/{len(tickers)} cotations OK "
            f"en {elapsed:.1f}s ({len(result.errors)} erreur(s), {max_workers} workers)"
        )
        return result
