"""Parser CSV pour importer un historique de cotations.

Problème résolu : le collector quotidien n'insère qu'1 quote/jour/ticker. Un
projet qui démarre a donc 0 historique — les features techniques (MA50,
Bollinger, 52w high/low, momentum) ne deviennent calculables qu'au bout de
2-3 mois. Ce parser permet un backfill one-shot depuis un CSV exporté
manuellement (Sika, BRVM officiel, broker, Bloomberg, etc.).

Principe : **tolérance max sur le format** — un utilisateur qui exporte des
données d'un site africain ou d'un broker local ne doit pas avoir à se battre
avec le délimiteur ou le format de date.

Détection auto :
  - Délimiteur : `,` / `;` / `\\t` (via `csv.Sniffer` + fallback heuristique)
  - Décimal : `.` ou `,` (heuristique : si plusieurs `,` par nombre, c'est le
    délimiteur ; si un seul `,` suivi de 1-6 chiffres, c'est le décimal FR)
  - Formats de date : ISO `YYYY-MM-DD`, FR `DD/MM/YYYY`, FR court `DD/MM/YY`,
    US `MM/DD/YYYY`, compact `YYYYMMDD`
  - En-têtes : case-insensitive, alias FR/EN (date/Date/DATE, close/clôture/cours,
    volume/Volume/Titres, etc.)

Colonnes :
  - **Obligatoires** : `date`, `close`
  - **Optionnelles** : `open`, `high`, `low`, `volume`, `value_traded`,
    `variation_pct`, `ticker` (ignoré si fourni via l'endpoint)

Le parser ne lève jamais sur une ligne cassée — il collecte les erreurs dans
`ImportResult.errors` pour affichage utilisateur. Seule une erreur fatale
(fichier vide, pas de colonne close) lève `ImportCsvError`.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)


# --- Data classes -----------------------------------------------------------

@dataclass
class HistoricalQuoteRow:
    """Ligne de cotation parsée — prête à être insérée dans `quotes`."""
    quote_date: datetime
    close_price: float
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    volume: int = 0
    value_traded: float = 0.0
    variation_pct: float | None = None


@dataclass
class ImportResult:
    """Résumé d'un parse. Tous les compteurs cumulent sur l'ensemble du fichier."""
    rows: list[HistoricalQuoteRow] = field(default_factory=list)
    skipped: int = 0          # lignes ignorées (format invalide, doublons internes)
    errors: list[str] = field(default_factory=list)  # messages lisibles utilisateur
    detected_delimiter: str = ","
    detected_columns: dict[str, str] = field(default_factory=dict)  # logical → raw header


class ImportCsvError(ValueError):
    """Erreur fatale : fichier vide, colonnes manquantes, encodage incompatible."""


# --- Header aliases ---------------------------------------------------------

# Chaque clé logique accepte plusieurs alias (case-insensitive, accents stripés).
# Ordre dans la liste = priorité (premier match gagne).
_COLUMN_ALIASES: dict[str, list[str]] = {
    "date": [
        "date", "quote_date", "trade_date", "séance", "seance", "jour", "day",
        "timestamp", "time",
    ],
    "close": [
        "close", "clôture", "cloture", "cours", "cours_cloture", "last", "price",
        "closing_price", "close_price", "adj_close", "fermeture",
    ],
    "open": ["open", "ouverture", "opening_price", "open_price"],
    "high": ["high", "plus_haut", "haut", "max", "highest"],
    "low": ["low", "plus_bas", "bas", "min", "lowest"],
    "volume": [
        "volume", "vol", "titres", "nb_titres", "volume_titres", "quantite",
        "quantité", "shares", "volume_shares",
    ],
    "value_traded": [
        "value_traded", "valeur_echangee", "valeur_échangée", "valeur_traitee",
        "valeur_traitée", "valeur", "capital_echange", "capital_échangé",
        "capital_traded", "amount", "turnover",
    ],
    "variation_pct": [
        "variation", "variation_pct", "var", "var_pct", "var%", "pct_change",
        "change_pct", "change", "variation_%",
    ],
    "ticker": ["ticker", "symbol", "symbole", "code", "isin"],
}

# --- Date parsing -----------------------------------------------------------

_DATE_FORMATS: list[str] = [
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%m/%d/%Y",           # US — en dernier pour favoriser FR
    "%Y%m%d",             # compact
    "%d %b %Y",           # "15 janv. 2024" (approximatif — locale dépendant)
]


def _strip_accents(s: str) -> str:
    """Normalise pour le match des en-têtes : strip accents + lowercase + strip."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _parse_date(raw: str) -> datetime | None:
    """Essaie chaque format — retourne None si aucun ne match."""
    if not raw:
        return None
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            # On normalise à minuit UTC (convention du projet : datetimes tz-aware,
            # la granularité quotidienne n'a pas besoin de l'heure réelle).
            return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_float(raw: str, decimal_is_comma: bool = False) -> float | None:
    """Parse un float avec tolérance : whitespace, espaces milliers, décimal FR."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Retire les espaces (séparateur de milliers FR typique "1 234,56")
    s = s.replace("\xa0", "").replace(" ", "")
    # Retire les séparateurs de milliers évidents
    if decimal_is_comma:
        # FR : "1.234,56" → "1234.56"
        s = s.replace(".", "").replace(",", ".")
    else:
        # EN : "1,234.56" → "1234.56"
        s = s.replace(",", "")
    # Signes + / - devant chiffres, parenthèses comptables "(123)" = -123
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    s = s.replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def _detect_decimal_is_comma(sample_values: Iterable[str]) -> bool:
    """Heuristique : si la majorité des valeurs numériques ont `,` comme séparateur
    décimal (1-6 décimales après la virgule) sans point, on considère FR."""
    fr_hits = 0
    en_hits = 0
    for v in sample_values:
        if not v or not isinstance(v, str):
            continue
        s = v.strip().replace("\xa0", "").replace(" ", "")
        # "1234,56" ou "1.234,56" → FR
        if re.search(r",\d{1,6}\s*$", s):
            fr_hits += 1
        # "1234.56" ou "1,234.56" → EN
        elif re.search(r"\.\d{1,6}\s*$", s):
            en_hits += 1
    return fr_hits > en_hits


# --- Column detection -------------------------------------------------------

def _match_column(raw_header: str) -> str | None:
    """Retourne la clé logique (date/close/...) ou None si aucun alias ne match."""
    norm = _strip_accents(raw_header)
    for logical, aliases in _COLUMN_ALIASES.items():
        if norm in aliases:
            return logical
    return None


def _build_column_map(headers: list[str]) -> dict[str, int]:
    """Retourne `{logical_key: column_index}`. Un même logical ne peut être
    assigné qu'une fois — première colonne matchante gagne."""
    mapping: dict[str, int] = {}
    for idx, h in enumerate(headers):
        if not h:
            continue
        logical = _match_column(h)
        if logical and logical not in mapping:
            mapping[logical] = idx
    return mapping


# --- Delimiter detection ----------------------------------------------------

def _detect_delimiter(sample: str) -> str:
    """csv.Sniffer + fallback heuristique (Sniffer peut se tromper sur petits
    échantillons ou lignes très hétérogènes)."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        pass
    # Fallback : choisit le délimiteur le plus fréquent sur la première ligne
    first_line = sample.split("\n", 1)[0]
    counts = {d: first_line.count(d) for d in [";", ",", "\t", "|"]}
    best = max(counts.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else ","


# --- Main entry -------------------------------------------------------------

def parse_historical_csv(
    content: str | bytes,
    *,
    strict: bool = False,
) -> ImportResult:
    """Parse un CSV d'historique de cotations et retourne `ImportResult`.

    Args:
        content: contenu brut (str ou bytes UTF-8/Latin-1).
        strict: si True, lève `ImportCsvError` dès la 1ère ligne invalide.
                Par défaut on collecte dans `errors` et continue.

    Raises:
        ImportCsvError: fichier vide, colonnes `date` ou `close` absentes.
    """
    if isinstance(content, bytes):
        # Tolérance encoding : UTF-8 par défaut, fallback Latin-1 (fréquent
        # sur exports Windows/Excel).
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = content.decode("latin-1", errors="replace")
    content = content.strip()
    if not content:
        raise ImportCsvError("Fichier vide.")

    sample = content[:4096]
    delimiter = _detect_delimiter(sample)

    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        raise ImportCsvError("Fichier sans en-tête.") from None

    col_map = _build_column_map(headers)
    if "date" not in col_map:
        raise ImportCsvError(
            f"Colonne date introuvable. Colonnes détectées : {headers!r}. "
            f"Alias acceptés : date, quote_date, séance, jour, timestamp, …",
        )
    if "close" not in col_map:
        raise ImportCsvError(
            f"Colonne close introuvable. Colonnes détectées : {headers!r}. "
            f"Alias acceptés : close, clôture, cours, last, price, …",
        )

    # Détection décimale : on lit les 20 premières lignes pour échantillonner.
    rows_cache: list[list[str]] = list(reader)
    close_idx = col_map["close"]
    sample_closes = [
        r[close_idx] for r in rows_cache[:20]
        if len(r) > close_idx and r[close_idx]
    ]
    decimal_is_comma = _detect_decimal_is_comma(sample_closes)

    result = ImportResult(
        detected_delimiter=delimiter,
        detected_columns={k: headers[v] for k, v in col_map.items()},
    )

    seen_dates: set[datetime] = set()

    for line_no, row in enumerate(rows_cache, start=2):  # start=2 → ligne 1 = header
        if not row or all(not c.strip() for c in row):
            # Ligne vide → skip silencieux
            continue

        try:
            parsed = _parse_row(
                row, col_map,
                line_no=line_no,
                decimal_is_comma=decimal_is_comma,
            )
        except _RowError as e:
            result.skipped += 1
            msg = f"Ligne {line_no}: {e}"
            if strict:
                raise ImportCsvError(msg) from e
            result.errors.append(msg)
            continue

        # Dédup interne — on garde la 1ère occurrence (le CSV peut avoir
        # plusieurs lignes pour la même date en cas d'export bugué).
        if parsed.quote_date in seen_dates:
            result.skipped += 1
            continue
        seen_dates.add(parsed.quote_date)
        result.rows.append(parsed)

    if not result.rows and not result.errors:
        raise ImportCsvError(
            "Aucune ligne de données après l'en-tête (fichier vide ou toutes "
            "les lignes sont en erreur).",
        )

    return result


class _RowError(ValueError):
    """Erreur interne non-fatale — catchée pour accumulation dans `errors`."""


def _parse_row(
    row: list[str],
    col_map: dict[str, int],
    *,
    line_no: int,
    decimal_is_comma: bool,
) -> HistoricalQuoteRow:
    """Parse une ligne — lève `_RowError` si format invalide."""
    def _cell(key: str) -> str:
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    date_raw = _cell("date")
    if not date_raw:
        raise _RowError("date vide")
    quote_date = _parse_date(date_raw)
    if quote_date is None:
        raise _RowError(f"date invalide {date_raw!r}")

    close_raw = _cell("close")
    close = _parse_float(close_raw, decimal_is_comma=decimal_is_comma)
    if close is None or close <= 0:
        raise _RowError(f"close invalide {close_raw!r}")

    open_ = _parse_float(_cell("open"), decimal_is_comma) if "open" in col_map else None
    high = _parse_float(_cell("high"), decimal_is_comma) if "high" in col_map else None
    low = _parse_float(_cell("low"), decimal_is_comma) if "low" in col_map else None
    vol_raw = _cell("volume") if "volume" in col_map else ""
    volume = 0
    if vol_raw:
        vol_f = _parse_float(vol_raw, decimal_is_comma)
        if vol_f is not None and vol_f >= 0:
            volume = int(vol_f)
    value_traded = 0.0
    if "value_traded" in col_map:
        vt = _parse_float(_cell("value_traded"), decimal_is_comma)
        if vt is not None and vt >= 0:
            value_traded = vt

    variation_pct = None
    if "variation_pct" in col_map:
        var_raw = _cell("variation_pct")
        # Strip d'un éventuel suffixe "%"
        var_raw_clean = var_raw.rstrip("%").strip() if var_raw else ""
        variation_pct = _parse_float(var_raw_clean, decimal_is_comma)

    return HistoricalQuoteRow(
        quote_date=quote_date,
        close_price=close,
        open_price=open_,
        high_price=high,
        low_price=low,
        volume=volume,
        value_traded=value_traded,
        variation_pct=variation_pct,
    )


__all__ = [
    "HistoricalQuoteRow",
    "ImportResult",
    "ImportCsvError",
    "parse_historical_csv",
]

# Suppress unused import that might be flagged.
_ = timezone  # noqa: F401 — imported explicitly for future use
