"""Valide Brevo SMTP en envoyant un VRAI email de test via EmailSender.

Le destinataire est lu depuis la table `recipients` (channel='email'), qui est
seedée au 1er boot depuis `EMAIL_TO` dans `.env`. Si la DB est vide, le script
seede lui-même avant d'envoyer.

Usage :
    .venv/bin/python scripts/check_brevo.py
    .venv/bin/python scripts/check_brevo.py --to dest@example.com
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Ajoute la racine projet au sys.path pour imports `src.*`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


REQUIRED = ["BREVO_SMTP_USER", "BREVO_SMTP_PASSWORD", "EMAIL_FROM"]


def _check_env() -> list[str]:
    return [k for k in REQUIRED if not os.getenv(k)]


def _ensure_recipient(to_address: str | None) -> str:
    """Crée (si besoin) un recipient email. Retourne l'adresse utilisée.

    - `--to` prioritaire
    - sinon : premier recipient actif en DB
    - sinon : EMAIL_TO de l'env (et seed dans la DB)
    """
    from sqlalchemy import select

    from src.database import get_session, init_db
    from src.models import Recipient

    init_db()  # crée les tables si elles n'existent pas encore (dev local)

    with get_session() as s:
        if to_address:
            existing = s.execute(
                select(Recipient)
                .where(Recipient.channel == "email")
                .where(Recipient.address == to_address)
            ).scalar_one_or_none()
            if existing:
                if not existing.enabled:
                    existing.enabled = True
                return to_address
            s.add(Recipient(channel="email", address=to_address, notes="Créé via check_brevo.py --to"))
            return to_address

        first = s.execute(
            select(Recipient)
            .where(Recipient.channel == "email")
            .where(Recipient.enabled.is_(True))
            .order_by(Recipient.id)
            .limit(1)
        ).scalar_one_or_none()
        if first:
            return first.address

        env_to = os.getenv("EMAIL_TO", "").strip()
        if not env_to:
            raise RuntimeError(
                "Aucun recipient email en DB et EMAIL_TO vide dans .env. "
                "Passe --to dest@example.com ou remplis EMAIL_TO."
            )
        s.add(Recipient(channel="email", address=env_to, notes="Seed initial depuis EMAIL_TO"))
        return env_to


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="Adresse email à ajouter/utiliser (sinon lu depuis DB/env)")
    args = parser.parse_args()

    if missing := _check_env():
        print(f"✗ Variables manquantes dans .env : {', '.join(missing)}")
        return 1

    try:
        dest = _ensure_recipient(args.to)
    except Exception as e:
        print(f"✗ Setup recipient : {e}")
        return 1

    from src.dates import format_date_fr
    from src.delivery.email_brevo import EmailSender, render_email_html
    from src.delivery.sample_brief import sample_brief, sample_snapshot

    tz = ZoneInfo(os.getenv("TIMEZONE", "Africa/Abidjan"))
    date_str = format_date_fr(datetime.now(tz))
    subject, html = render_email_html(
        sample_brief(),
        date_str,
        market_snapshot=sample_snapshot(),
        edition_num="TEST",
    )
    subject = f"[TEST] {subject}"

    print("Config SMTP :")
    print(f"  host       = {os.getenv('BREVO_SMTP_HOST', 'smtp-relay.brevo.com')}:"
          f"{os.getenv('BREVO_SMTP_PORT', '587')}")
    print(f"  user       = {os.getenv('BREVO_SMTP_USER')}")
    print(f"  from       = {os.getenv('EMAIL_FROM_NAME', 'BRVM Agent')} "
          f"<{os.getenv('EMAIL_FROM')}>")
    print(f"  to         = {dest}  (depuis la table `recipients`)")
    print(f"  subject    = {subject}")
    print(f"  html size  = {len(html)} bytes")
    print()

    print("Envoi en cours…")
    try:
        sent = EmailSender().send(subject, html)
    except Exception as e:
        print(f"✗ {type(e).__name__}: {e}")
        if "Auth" in type(e).__name__ or "535" in str(e):
            print("  → Vérifier BREVO_SMTP_USER (xxx@smtp-brevo.com) + BREVO_SMTP_PASSWORD.")
        if "Sender" in type(e).__name__ or "550" in str(e):
            print("  → EMAIL_FROM doit appartenir à un domaine authentifié dans Brevo.")
        return 2

    print(f"✓ Email envoyé à {sent}")
    print("  → Vérifier la boîte (et le dossier spam la 1re fois).")
    print("  → Si reçu avec la charte Lexend/navy/or : Brevo OK, on passe à Sentry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
