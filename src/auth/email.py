"""Envoi du magic link par email (Brevo SMTP)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import get_settings

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
    """Envoie le magic link via le même SMTP que les briefs."""
    settings = get_settings()
    html = _env.get_template("magic_link.html.j2").render(
        link=link,
        ttl_minutes=ttl_minutes,
        name=name,
        ip=ip,
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Connexion à la console BRVM Agent"
    msg["From"] = formataddr((settings.email_from_name, settings.email_from))
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(settings.brevo_smtp_host, settings.brevo_smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.brevo_smtp_user, settings.brevo_smtp_password)
        server.send_message(msg)
    logger.info(f"Magic link envoyé à {to_email}")
