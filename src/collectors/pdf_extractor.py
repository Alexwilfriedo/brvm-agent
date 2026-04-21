"""Téléchargement + extraction texte pour PDFs distants.

Utilitaire isolé pour pouvoir être réutilisé par d'autres collectors (BOAD,
BCEAO, etc.) et testé indépendamment.

Philosophie :
  - Jamais de retry silencieux — le retry est géré par l'appelant (collector)
    qui sait quel PDF peut être skippé sans bloquer le run global.
  - Protection défensive contre les PDFs piégés : cap `max_size_mb` appliqué
    AVANT de lire tout le body, via `stream=True` + lecture incrémentale.
  - Retourne `""` (pas d'exception) sur PDF image-only ou chiffré — ces cas
    sont attendus et non-bloquants.
  - Lève `PdfExtractionError` sur PDF corrompu, 4xx/5xx, timeout, taille
    excessive. Le collector appelant log et skip l'item.
"""
from __future__ import annotations

import io
import logging
import re

import pdfplumber
import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)

# Valeurs par défaut — surchargeables par source via `config`.
DEFAULT_TIMEOUT_S = 20
DEFAULT_MAX_SIZE_MB = 10
DEFAULT_MAX_CHARS = 15_000

_WHITESPACE_RUN = re.compile(r"\n{3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")


class PdfExtractionError(RuntimeError):
    """Erreur durable (corruption, réseau, taille excessive, auth requise, …).

    Les appelants doivent catcher et skipper l'item, pas faire échouer le run.
    """


def download_pdf(
    url: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
) -> bytes:
    """Télécharge un PDF en streaming, en coupant si > `max_size_mb`.

    Raises:
        PdfExtractionError: timeout, statut non-2xx, taille excessive,
            content-type incompatible, ou erreur réseau.
    """
    max_bytes = max_size_mb * 1024 * 1024
    try:
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"},
            timeout=timeout_s,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                raise PdfExtractionError(f"HTTP {resp.status_code} sur {url}")

            # Content-Length est optionnel ; s'il existe, on peut fail-fast.
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise PdfExtractionError(
                    f"PDF trop volumineux ({content_length} bytes > "
                    f"{max_size_mb} MB) : {url}"
                )

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise PdfExtractionError(
                        f"PDF dépasse {max_size_mb} MB en streaming : {url}"
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
    except requests.RequestException as e:
        raise PdfExtractionError(f"Réseau/timeout sur {url} : {e}") from e

    # Sanity check : le header d'un PDF est "%PDF-"
    if not body.startswith(b"%PDF-"):
        raise PdfExtractionError(
            f"Réponse non-PDF pour {url} (premiers bytes : {body[:8]!r})"
        )
    return body


def extract_text(pdf_bytes: bytes, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Extrait le texte d'un PDF binaire.

    - PDF chiffré ou image-only → retourne `""` (cas non-bloquants).
    - PDF corrompu → raise `PdfExtractionError`.
    - Résultat toujours tronqué à `max_chars` caractères.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return ""
            parts: list[str] = []
            for page in pdf.pages:
                # pdfplumber retourne None si page image-only
                page_text = page.extract_text() or ""
                if page_text.strip():
                    parts.append(page_text)
                # Early-stop dès qu'on a assez de matière — évite de parser
                # 80 pages d'annexes si la substance tient dans les 6 premières.
                if sum(len(p) for p in parts) >= max_chars:
                    break
    except Exception as e:
        # pdfplumber lève divers types selon la corruption ; on homogénéise.
        if "encrypt" in str(e).lower() or "password" in str(e).lower():
            logger.info(f"PDF chiffré (skip) : {e}")
            return ""
        raise PdfExtractionError(f"Extraction pdfplumber échouée : {e}") from e

    if not parts:
        # Probablement un PDF image-only (scanné). v1 : pas d'OCR.
        return ""

    raw = "\n\n".join(parts)
    # Normalisation whitespace
    raw = _TRAILING_SPACES.sub("\n", raw)
    raw = _WHITESPACE_RUN.sub("\n\n", raw).strip()
    return raw[:max_chars]


def fetch_and_extract(
    url: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Combine download + extract. Raises `PdfExtractionError` à tout niveau."""
    body = download_pdf(url, timeout_s=timeout_s, max_size_mb=max_size_mb)
    return extract_text(body, max_chars=max_chars)
