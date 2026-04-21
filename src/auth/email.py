"""Envoi du magic link par email.

Utilise le même transport que les briefs (via `EmailSender._send_one_recipient`)
pour bénéficier automatiquement du toggle SMTP/HTTP (`EMAIL_TRANSPORT`).
"""
from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..delivery.email_brevo import EmailSender

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "delivery" / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def send_magic_link(
    *,
    to_email: str,
    link: str,
    ttl_minutes: int,
    name: str | None = None,
    ip: str | None = None,
) -> None:
    """Envoie le magic link via le transport Brevo configuré (SMTP ou HTTP)."""
    html = _env.get_template("magic_link.html.j2").render(
        link=link,
        ttl_minutes=ttl_minutes,
        name=name,
        ip=ip,
    )
    EmailSender()._send_one_recipient(
        subject="Connexion à la console BRVM Agent",
        html=html,
        address=to_email,
        name=name,
    )
    logger.info(f"Magic link envoyé à {to_email}")
