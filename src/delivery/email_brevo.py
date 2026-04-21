"""Livraison email via SMTP Brevo.

Le rendu HTML est délégué à un template Jinja2 (`templates/brief_email.html.j2`)
pour pouvoir itérer sur la charte graphique sans toucher au code Python.

Destinataires : lus depuis la table `recipients` (channel="email", enabled=True).
Gestion via l'API admin `/api/recipients`.

Preview : un brief d'exemple est exposé via `/preview/brief` pour valider la
charte sans envoyer d'email (cf. `src/api/preview.py`).
"""
from __future__ import annotations

import logging
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..analysis.schemas import BriefPayload
from ..config import get_settings
from .repository import active_recipients

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# --- Design tokens (charte graphique) ---------------------------------------

DIRECTION_STYLES: dict[str, dict[str, str]] = {
    "buy":    {"label": "Achat",      "bg": "#059669", "fg": "#FFFFFF", "accent": "#059669"},
    "watch":  {"label": "Surveiller", "bg": "#D97706", "fg": "#FFFFFF", "accent": "#D97706"},
    "hold":   {"label": "Conserver",  "bg": "#475569", "fg": "#FFFFFF", "accent": "#475569"},
    "reduce": {"label": "Alléger",    "bg": "#B45309", "fg": "#FFFFFF", "accent": "#B45309"},
    "avoid":  {"label": "Éviter",     "bg": "#DC2626", "fg": "#FFFFFF", "accent": "#DC2626"},
}

REGIME_STYLES: dict[str, dict[str, str]] = {
    "trend_up":     {"label": "Tendance haussière", "bg": "#DCFCE7", "fg": "#14532D"},
    "trend_down":   {"label": "Tendance baissière", "bg": "#FEE2E2", "fg": "#7F1D1D"},
    "range":        {"label": "Range",              "bg": "#F1F5F9", "fg": "#334155"},
    "risk_off":     {"label": "Risk-off",           "bg": "#FEF3C7", "fg": "#78350F"},
    "event_driven": {"label": "Event-driven",       "bg": "#E0E7FF", "fg": "#312E81"},
    "illiquid":     {"label": "Illiquide",          "bg": "#FFEDD5", "fg": "#7C2D12"},
}


def _direction_style(direction: str | None) -> dict[str, str]:
    return DIRECTION_STYLES.get(direction or "watch", DIRECTION_STYLES["watch"])


def _regime_style(regime: str | None) -> dict[str, str] | None:
    if not regime:
        return None
    return REGIME_STYLES.get(regime)


# --- Rendu ------------------------------------------------------------------

def render_email_html(
    brief_raw: dict,
    date_str: str,
    *,
    market_snapshot: dict | None = None,
    edition_num: int | str = "001",
    app_version: str = "v0.1",
    revision: int = 1,
) -> tuple[str, str]:
    """Retourne (sujet, html) à partir du dict JSON du brief.

    Args:
        brief_raw: payload brut produit par Opus (synthesis.py).
        date_str: date formatée en français ("Lundi 21 avril 2026").
        market_snapshot: snapshot des cotations (top gainers/losers).
        edition_num: numéro d'édition affiché dans l'en-tête.
        app_version: affiché en pied de page.
        revision: numéro de révision (1 = première émission). Si > 1, le
                  sujet et le template mentionnent explicitement la révision.
    """
    brief = BriefPayload.from_raw(brief_raw)

    subject_prefix = f"[Révision {revision}] " if revision > 1 else ""
    subject = f"{subject_prefix}Brief BRVM · {date_str}"
    if brief.is_error:
        subject = f"[DEGRADÉ] {subject}"
    elif not brief.opportunities:
        subject += " · aucun signal fort"
    else:
        top = brief.opportunities[0]
        subject += f" · {top.ticker} {_direction_style(top.direction)['label'].lower()}"

    preheader = (
        brief.market_summary[:120]
        if brief.market_summary
        else f"{len(brief.opportunities)} opportunité(s), {len(brief.alerts)} alerte(s)"
    )

    template = _env.get_template("brief_email.html.j2")
    html = template.render(
        subject=subject,
        preheader=preheader,
        date_str=date_str,
        edition_num=edition_num,
        app_version=app_version,
        revision=revision,
        brief=brief,
        snapshot=market_snapshot,
        regime=_regime_style(brief.market_regime),
        direction_style=_direction_style,
    )
    return subject, html


# --- SMTP -------------------------------------------------------------------

class NoRecipientError(RuntimeError):
    """Aucun destinataire email actif en DB — la livraison ne peut pas avoir lieu."""


# Erreurs SMTP qu'on considère comme **transitoires** (retry pertinent) :
# - socket.timeout : Brevo n'a pas répondu (TCP connect ou session)
# - ConnectionError : refus TCP, connexion fermée prématurément
# - SMTPServerDisconnected : le serveur a coupé en cours de session
# - SMTPConnectError : échec TCP initial côté smtplib
#
# On ne retry PAS les erreurs métier (auth invalide, format adresse cassé,
# contenu rejeté) — elles ne se règlent pas toutes seules.
_TRANSIENT_SMTP = (
    socket.timeout,
    ConnectionError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
)

# Politique de retry SMTP — même pattern que `analysis/_retry.py::anthropic_retry`.
# 4 tentatives avec wait exponentiel 5s → 10s → 20s → 40s (capped).
# Temps pire cas cumulé : 4×30s (timeout) + 5+10+20 = ~155s. Acceptable pour
# un brief quotidien.
_smtp_retry = retry(
    retry=retry_if_exception_type(_TRANSIENT_SMTP),
    wait=wait_exponential(multiplier=5, min=5, max=40),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class EmailSender:
    """Envoie un brief à tous les recipients actifs (channel='email').

    Une seule session SMTP est ouverte pour tous les destinataires.
    Retry automatique (tenacity) en cas d'erreur SMTP transitoire.
    """

    def __init__(self):
        self.s = get_settings()

    def send(self, subject: str, html: str) -> list[str]:
        """Envoie à tous les recipients email actifs. Retourne la liste
        des adresses servies.

        Retry jusqu'à 4 fois avec backoff exponentiel si Brevo timeout ou
        ferme la connexion. Les erreurs métier (auth, format) remontent
        immédiatement sans retry.
        """
        recipients = active_recipients("email")
        if not recipients:
            raise NoRecipientError(
                "Aucun recipient email actif en DB. "
                "Ajoute-en via POST /api/recipients ou via EMAIL_TO dans .env (seed)."
            )
        return self._send_once(subject, html, recipients)

    @_smtp_retry
    def _send_once(
        self, subject: str, html: str, recipients: list[tuple[str, str]]
    ) -> list[str]:
        sent_to: list[str] = []
        with smtplib.SMTP(self.s.brevo_smtp_host, self.s.brevo_smtp_port, timeout=30) as server:
            server.starttls()
            server.login(self.s.brevo_smtp_user, self.s.brevo_smtp_password)

            for address, name in recipients:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = formataddr((self.s.email_from_name, self.s.email_from))
                msg["To"] = formataddr((name, address)) if name else address
                msg.attach(MIMEText(html, "html", "utf-8"))
                server.send_message(msg)
                sent_to.append(address)

        logger.info(f"Email envoyé à {len(sent_to)} destinataire(s) : {', '.join(sent_to)}")
        return sent_to
