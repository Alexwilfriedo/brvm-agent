"""Livraison email via SMTP Brevo.

Le rendu HTML est délégué à un template Jinja2 (`templates/brief_email.html.j2`)
pour pouvoir itérer sur la charte graphique sans toucher au code Python.

Destinataires : lus depuis la table `recipients` (channel="email", enabled=True).
Gestion via l'API admin `/api/recipients`.

Preview : un brief d'exemple est exposé via `/preview/brief` pour valider la
charte sans envoyer d'email (cf. `src/api/preview.py`).

Note retry : tenacity est appliqué **par destinataire** et non au niveau de la
boucle complète. Avec un wrap au niveau boucle, un timeout sur le 3e envoi
déclenchait un retry qui re-livrait les destinataires 1 et 2 → doublons
garantis. Ici chaque destinataire a sa propre session SMTP courte, isolée.
"""
from __future__ import annotations

import logging
import os
import smtplib
import socket
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

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
    """Retourne (sujet, html) à partir du dict JSON du brief."""
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


_TRANSIENT_SMTP = (
    socket.timeout,
    ConnectionError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
)

# Retry appliqué **par destinataire** : évite le bug doublons d'un retry au
# niveau boucle (qui re-livrait les destinataires déjà servis en cas d'échec
# mid-session).
_smtp_retry = retry(
    retry=retry_if_exception_type(_TRANSIENT_SMTP),
    wait=wait_exponential(multiplier=5, min=5, max=40),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class EmailSender:
    """Envoie un brief à tous les recipients actifs (channel='email') via SMTP Brevo."""

    def __init__(self):
        self.s = get_settings()

    def send(self, subject: str, html: str) -> list[str]:
        """Envoie à tous les recipients email actifs. Retourne la liste
        des adresses servies.

        Raises:
            NoRecipientError: aucun destinataire actif en DB.
            Exception: si TOUS les envois unitaires échouent après retry.
        """
        recipients = active_recipients("email")
        if not recipients:
            raise NoRecipientError(
                "Aucun recipient email actif en DB. "
                "Ajoute-en via POST /api/recipients ou via EMAIL_TO dans .env (seed)."
            )

        sent: list[str] = []
        errors: list[str] = []
        for address, name in recipients:
            try:
                self._send_one_recipient(subject, html, address, name)
                sent.append(address)
            except Exception as e:
                errors.append(f"{address}: {type(e).__name__}: {e}")
                logger.error(f"Envoi SMTP échoué pour {address} : {e}")

        if sent:
            logger.info(
                f"Email envoyé à {len(sent)}/{len(recipients)} destinataire(s) "
                f": {', '.join(sent)}"
            )
            if errors:
                logger.warning(f"Envois partiellement échoués : {'; '.join(errors)}")
            return sent

        # Aucun envoi réussi → erreur dure pour que le pipeline marque `email_ok=False`.
        raise RuntimeError(f"Tous les envois SMTP ont échoué : {'; '.join(errors)}")

    @_smtp_retry
    def _send_one_recipient(
        self, subject: str, html: str, address: str, name: str | None,
    ) -> None:
        """Une session SMTP par destinataire. Retry automatique sur erreur transitoire."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((self.s.email_from_name, self.s.email_from))
        msg["To"] = formataddr((name, address)) if name else address
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(self.s.brevo_smtp_host, self.s.brevo_smtp_port, timeout=30) as server:
            server.starttls()
            server.login(self.s.brevo_smtp_user, self.s.brevo_smtp_password)
            server.send_message(msg)
        logger.info(f"SMTP OK pour {address}")


# --- Test email au démarrage ------------------------------------------------

def send_startup_test_email() -> None:
    """Envoie un email de test minimal au 1er recipient actif (ou à `EMAIL_TO`).

    Utilisé au démarrage de l'app (gated par `SEND_STARTUP_TEST_EMAIL=true`)
    pour valider la chaîne Brevo sans attendre le cron quotidien. Ne lève
    jamais — log explicite sur succès/échec pour que la cause soit visible
    immédiatement dans les logs Railway.
    """
    s = get_settings()
    try:
        recipients = active_recipients("email")
    except Exception as e:
        logger.error(f"Test startup : impossible de lire les recipients ({e})")
        return

    target: tuple[str, str | None] | None = None
    if recipients:
        target = recipients[0]
    elif s.email_to:
        target = (s.email_to, None)
    else:
        logger.warning(
            "Test startup : aucun recipient actif et EMAIL_TO vide — skip."
        )
        return

    to_email, to_name = target
    tz = ZoneInfo(os.getenv("TIMEZONE", "Africa/Abidjan"))
    when = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = f"[TEST] BRVM Agent démarré — {when}"
    html = (
        f"<p>Ping de démarrage — BRVM Agent vient de booter.</p>"
        f"<p><strong>Sender (from)</strong> : {s.email_from}</p>"
        f"<p><strong>Recipient (to)</strong> : {to_email}</p>"
        f"<p><strong>Horodatage</strong> : {when}</p>"
        f"<p style='color:#64748B;font-size:12px'>"
        f"Désactive ce test en mettant <code>SEND_STARTUP_TEST_EMAIL=false</code> "
        f"(ou en retirant la variable) dans les envs."
        f"</p>"
    )
    try:
        # On envoie directement (1 recipient, 1 session SMTP) sans passer par
        # `active_recipients` — sinon on spam tous les destinataires au boot.
        sender = EmailSender()
        sender._send_one_recipient(subject, html, to_email, to_name)
        logger.info(
            f"Test startup OK — mail envoyé à {to_email}. "
            f"Si le mail n'arrive pas, consulte Brevo → Transactional → Logs."
        )
    except Exception as e:
        logger.error(
            f"Test startup ÉCHEC pour {to_email} : {type(e).__name__}: {e}. "
            f"Vérifie BREVO_SMTP_USER/PASSWORD et que EMAIL_FROM={s.email_from} "
            f"est un expéditeur/domaine validé dans Brevo."
        )
