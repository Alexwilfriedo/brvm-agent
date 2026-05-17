"""Extracteur des cotations depuis un bulletin BRVM officiel PDF.

**Stratégie positionnelle BRVM-spécifique** (remplace l'approche générique par
en-têtes qui était trop fragile sur les vrais PDFs).

Les bulletins BRVM exposent un tableau principal avec une structure stable aux
variations près de format :

- **Format 2023+** (16 colonnes) : une colonne de code sectoriel est ajoutée
  en position 0 (`CB`, `IND`, `FIN`, `ENE`, `TEL`, `CD`, `SPU`), décalant
  toutes les autres de +1.
- **Format pré-2023** (15 colonnes) : pas de code sectoriel, le ticker est
  en col 0.

L'extracteur détecte le layout **par ligne** en regardant si le ticker (string
3-5 lettres majuscules matchant le référentiel BRVM) est en col 0 ou col 1,
puis applique les positions correspondantes pour close / variation / volume /
valeur. Cette approche résiste aux pages multiples (les tables de continuation
n'ont pas d'en-tête et seraient impossibles à parser en mode header-based).

Le parser ne lève **jamais** — retourne une liste potentiellement vide et
collecte les erreurs dans `errors` pour log + affichage utilisateur.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pdfplumber

from ..collectors.historical_import import _parse_date
from ..collectors.sika_quotes import BRVM_TICKERS

logger = logging.getLogger(__name__)


# --- Data classes -----------------------------------------------------------

@dataclass
class BrvmPdfQuote:
    """Une ligne de cotation extraite du PDF."""
    ticker: str
    close_price: float
    variation_pct: float | None = None
    volume: int = 0
    value_traded: float = 0.0
    open_price: float | None = None
    previous_close: float | None = None


@dataclass
class BrvmPdfResult:
    """Sortie du parser PDF pour un bulletin."""
    quote_date: datetime | None
    quotes: list[BrvmPdfQuote] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_text_preview: str = ""
    parsed_from: str = ""


# --- Known tickers + sector codes ------------------------------------------

_KNOWN_TICKERS: set[str] = {t.ticker.upper() for t in BRVM_TICKERS}

# Codes sectoriels BRVM officiels (ceux qu'on voit dans le BOC 2023+).
_SECTOR_CODES: set[str] = {"CB", "CD", "ENE", "FIN", "IND", "TEL", "SPU"}


# --- Date extraction --------------------------------------------------------

_FR_MONTHS: dict[str, int] = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12,
}

_DATE_PATTERNS_TEXT: list[re.Pattern] = [
    # "N° 251 jeudi 30 décembre 2021" — le format dominant sur les BOCs.
    re.compile(r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+(\d{1,2})\s+(\w+)\s+(\d{4})", re.IGNORECASE),
    # "séance du 15 janvier 2024" — fallback pour les anciens bulletins.
    re.compile(r"séance\s+du\s+(?:\w+\s+)?(\d{1,2})\s+(\w+)\s+(\d{4})", re.IGNORECASE),
    # "15/01/2024" — dernier recours.
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"),
]


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _extract_date_from_text(text: str) -> datetime | None:
    """Parse 'jeudi 30 décembre 2021' → datetime UTC.

    Accepte plusieurs formats courants ; premier match gagne.
    """
    if not text:
        return None

    # Pattern FR long (nom du jour + jour + mois + année)
    for pat in _DATE_PATTERNS_TEXT[:2]:
        m = pat.search(text)
        if not m:
            continue
        try:
            day = int(m.group(1))
            month_name = _strip_accents(m.group(2))
            year = int(m.group(3))
            month = _FR_MONTHS.get(month_name)
            if month is None:
                continue
            return datetime(year, month, day, tzinfo=UTC)
        except (ValueError, IndexError):
            continue

    # Fallback numérique "DD/MM/YYYY" ou "DD-MM-YYYY"
    m = _DATE_PATTERNS_TEXT[2].search(text)
    if m:
        try:
            day = int(m.group(1))
            month = int(m.group(2))
            year = int(m.group(3))
            # Sanity : BRVM créée en 1998 → pas de date antérieure
            if 1 <= month <= 12 and 1 <= day <= 31 and 1998 <= year <= 2100:
                return datetime(year, month, day, tzinfo=UTC)
        except (ValueError, IndexError):
            pass
    return None


def _extract_date_from_filename(filename: str) -> datetime | None:
    """Déduit la date depuis le nom de fichier — patterns BRVM.

    Exemples reconnus :
      - boc_20260423_2.pdf → 2026-04-23
      - boc_du_15_01_2024.pdf → 15/01/2024
      - bulletin-2024-01-15.pdf → 2024-01-15
    """
    if not filename:
        return None
    candidates = [
        # YYYYMMDD compact (format dominant sur brvm.org)
        (re.compile(r"(\d{8})"), lambda s: s),
        # DD_MM_YYYY ou DD-MM-YYYY
        (re.compile(r"(\d{1,2})[_-](\d{1,2})[_-](\d{4})"),
         lambda m: f"{m[0]}/{m[1]}/{m[2]}"),
        # YYYY-MM-DD ou YYYY_MM_DD
        (re.compile(r"(\d{4})[_-](\d{1,2})[_-](\d{1,2})"),
         lambda m: f"{m[0]}-{m[1]}-{m[2]}"),
    ]
    for pat, normalize in candidates:
        m = pat.search(filename)
        if not m:
            continue
        if callable(normalize) and pat.groups > 1:
            raw = normalize(m.groups())
        else:
            raw = normalize(m.group(1))
        dt = _parse_date(raw)
        if dt:
            return dt
    return None


# --- Number parsing (FR/EN tolerant) ---------------------------------------

def _parse_fr_number(raw: str) -> float | None:
    """Parse '12 400,50' / '1,418,335.00' / '5,82 %' tolérant aux espaces et séparateurs.

    Convention BRVM : espaces = milliers, virgule = décimal. On gère aussi
    les exports US au cas où.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Retire les suffixes courants
    s = s.replace("%", "").strip()
    # Retire les espaces (incluant NBSP) — milliers en FR
    s = s.replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    # Parenthèses comptables (123) → -123
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]

    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        # Le dernier séparateur rencontré est le décimal
        if s.rindex(",") > s.rindex("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        # Virgule + 1-3 chiffres à la fin = décimal FR ; sinon séparateur de milliers
        parts = s.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 3:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


# --- Ticker resolution ------------------------------------------------------

def _is_plausible_ticker_cell(cell: str) -> bool:
    """Vrai si la cellule ressemble à un ticker BRVM connu (3-6 lettres majuscules)."""
    if not cell:
        return False
    s = str(cell).strip().upper()
    if not s:
        return False
    return s in _KNOWN_TICKERS


def _detect_ticker_column(row: list) -> int | None:
    """Retourne 0 ou 1 selon la position du ticker dans la ligne, None si aucune.

    - 2023+ : col 0 = code sectoriel (CB/IND/…), col 1 = ticker
    - pré-2023 : col 0 = ticker, col 1 = nom
    """
    if not row or len(row) < 3:
        return None

    def _cell(i: int) -> str:
        return str(row[i] or "").strip() if i < len(row) else ""

    c0 = _cell(0)
    c1 = _cell(1)

    # Priorité au format 2023+ : code sectoriel reconnu + ticker connu
    if c0.upper() in _SECTOR_CODES and _is_plausible_ticker_cell(c1):
        return 1
    # Format ancien : ticker directement en col 0
    if _is_plausible_ticker_cell(c0):
        return 0
    # Fallback : ticker en col 1 même sans code sectoriel connu
    if _is_plausible_ticker_cell(c1):
        return 1
    return None


# --- Row parsing ------------------------------------------------------------

def _parse_quotation_row(
    row: list,
    ticker_col: int,
    row_num: int,
    result: BrvmPdfResult,
) -> None:
    """Extrait une ligne de cotation à partir de positions fixes.

    Schéma (après décalage `ticker_col`) :
      ticker_col       → ticker (Symbole)
      ticker_col + 1   → nom (Titre)
      ticker_col + 2   → flag ex-d / ex-c (optionnel)
      ticker_col + 3   → Cours Précédent
      ticker_col + 4   → Ouverture
      ticker_col + 5   → Clôture           ← CLOSE
      ticker_col + 6   → Variation jour    ← VARIATION
      ticker_col + 7   → Volume            ← VOLUME
      ticker_col + 8   → Valeur            ← VALUE
    """
    def _cell(i: int) -> str:
        return str(row[i] or "").strip() if 0 <= i < len(row) else ""

    ticker = _cell(ticker_col).upper()
    if not _is_plausible_ticker_cell(ticker):
        return

    base = ticker_col
    prev_close = _parse_fr_number(_cell(base + 3))
    open_price = _parse_fr_number(_cell(base + 4))
    close = _parse_fr_number(_cell(base + 5))
    variation_raw = _cell(base + 6)
    volume_raw = _cell(base + 7)
    value_raw = _cell(base + 8)

    if close is None or close <= 0:
        # Ligne vide ou placeholder — on skip silencieux plutôt que remonter une
        # fausse erreur (les bulletins contiennent parfois des lignes de sépa).
        return

    variation = _parse_fr_number(variation_raw)
    vol_f = _parse_fr_number(volume_raw)
    volume = int(vol_f) if vol_f is not None and vol_f >= 0 else 0
    val_f = _parse_fr_number(value_raw)
    value_traded = val_f if val_f is not None and val_f >= 0 else 0.0

    result.quotes.append(BrvmPdfQuote(
        ticker=ticker,
        close_price=close,
        variation_pct=variation,
        volume=volume,
        value_traded=value_traded,
        open_price=open_price,
        previous_close=prev_close,
    ))
    _ = row_num  # réservé pour log de debug ultérieur


# --- Main extraction --------------------------------------------------------

def _extract_quotation_rows(pdf: pdfplumber.PDF, result: BrvmPdfResult) -> None:
    """Stratégie BRVM : on parcourt toutes les tables de toutes les pages et
    on pique chaque ligne où un ticker BRVM apparaît en col 0 ou col 1.

    Dédup par ticker — la 1ère occurrence gagne (les bulletins peuvent avoir
    des tables récap avec le même ticker + des chiffres différents ; on veut
    la ligne de cotation principale qui arrive en premier)."""
    seen_tickers: set[str] = set()

    for page_num, page in enumerate(pdf.pages, start=1):
        try:
            tables = page.extract_tables() or []
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"Page {page_num}: extract_tables a échoué ({e})")
            continue

        for t_idx, table in enumerate(tables):
            if not table or len(table) < 1:
                continue
            for row_idx, row in enumerate(table):
                ticker_col = _detect_ticker_column(row)
                if ticker_col is None:
                    continue
                ticker = str(row[ticker_col] or "").strip().upper()
                if ticker in seen_tickers:
                    continue
                before = len(result.quotes)
                _parse_quotation_row(row, ticker_col, row_idx, result)
                if len(result.quotes) > before:
                    seen_tickers.add(ticker)

    result.parsed_from = "positional"


def parse_brvm_pdf(content: bytes, *, filename: str = "") -> BrvmPdfResult:
    """Parse un bulletin BRVM PDF et retourne les cotations extraites.

    Ne lève jamais — collecte les erreurs dans `result.errors`.
    """
    result = BrvmPdfResult(quote_date=None)

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            # Date : texte des 2 premières pages d'abord (la date est parfois en
            # pied de page 1), puis nom de fichier en fallback.
            first_text = ""
            for page in pdf.pages[:2]:
                try:
                    first_text += (page.extract_text() or "") + "\n"
                except Exception as e:  # noqa: BLE001
                    result.errors.append(f"extract_text: {e}")
                    continue
            result.raw_text_preview = first_text[:800]
            date_from_text = _extract_date_from_text(first_text)
            date_from_name = _extract_date_from_filename(filename) if filename else None
            result.quote_date = date_from_text or date_from_name

            _extract_quotation_rows(pdf, result)
    except Exception as e:  # noqa: BLE001
        logger.exception("[brvm_pdf] erreur ouverture pdfplumber")
        result.errors.append(f"PDF illisible : {e}")
        return result

    if not result.quotes:
        result.errors.append(
            "Aucune cotation extraite. Le format du PDF n'est pas reconnu — "
            "ce n'est peut-être pas un bulletin BRVM standard.",
        )
    return result


__all__ = ["BrvmPdfQuote", "BrvmPdfResult", "parse_brvm_pdf"]
