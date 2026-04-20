"""Valide Wassoya en envoyant un VRAI message WhatsApp de test.

Pré-requis :
- `WASSOYA_API_KEY` défini dans .env
- `WASSOYA_SENDER_NUMBER` défini (ton numéro WA Business Wassoya, format 225...)
- `WASSOYA_TEMPLATE_NAME` défini (nom d'un template Meta approuvé via Wassoya)
- Au moins 1 recipient channel='whatsapp' en DB (ou passer --to)

Usage :
    .venv/bin/python scripts/check_wassoya.py
    .venv/bin/python scripts/check_wassoya.py --to +2250700000000
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


REQUIRED = ["WASSOYA_API_KEY", "WASSOYA_SENDER_NUMBER", "WASSOYA_TEMPLATE_NAME"]


def _ensure_recipient(to_address: str | None) -> str:
    from sqlalchemy import select

    from src.database import get_session, init_db
    from src.models import Recipient

    init_db()
    with get_session() as s:
        if to_address:
            existing = s.execute(
                select(Recipient)
                .where(Recipient.channel == "whatsapp")
                .where(Recipient.address == to_address)
            ).scalar_one_or_none()
            if existing:
                if not existing.enabled:
                    existing.enabled = True
                return to_address
            s.add(Recipient(channel="whatsapp", address=to_address,
                            notes="Créé via check_wassoya.py --to"))
            return to_address

        first = s.execute(
            select(Recipient)
            .where(Recipient.channel == "whatsapp")
            .where(Recipient.enabled.is_(True))
            .order_by(Recipient.id)
            .limit(1)
        ).scalar_one_or_none()
        if first:
            return first.address

        env_to = os.getenv("WHATSAPP_TO_NUMBER", "").strip()
        if not env_to:
            raise RuntimeError(
                "Aucun recipient WhatsApp en DB et WHATSAPP_TO_NUMBER vide. "
                "Passe --to +2250700000000 ou remplis WHATSAPP_TO_NUMBER dans .env."
            )
        s.add(Recipient(channel="whatsapp", address=env_to,
                        notes="Seed depuis WHATSAPP_TO_NUMBER"))
        return env_to


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="Numéro E.164 (ex: +2250700000000)")
    args = parser.parse_args()

    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        print(f"✗ Variables manquantes : {', '.join(missing)}")
        print("  → Crée d'abord un template Meta via l'interface Wassoya et mets son nom dans WASSOYA_TEMPLATE_NAME.")
        return 1

    try:
        dest = _ensure_recipient(args.to)
    except Exception as e:
        print(f"✗ Setup recipient : {e}")
        return 1

    from src.delivery.sample_brief import sample_brief
    from src.delivery.whatsapp import WhatsAppSender, format_brief_short

    brief = sample_brief()
    preview = format_brief_short(brief)

    sender = WhatsAppSender()
    print("Config Wassoya :")
    print(f"  base url     = {os.getenv('WASSOYA_API_BASE_URL', 'https://api.wassoya.com')}")
    print(f"  from         = {os.getenv('WASSOYA_SENDER_NUMBER')}")
    print(f"  template     = {os.getenv('WASSOYA_TEMPLATE_NAME')}")
    print(f"  to           = {dest}  (depuis la table `recipients`)")
    print(f"  enabled      = {sender.enabled}")
    print(f"  text preview ({len(preview)} chars) :")
    print("  " + "\n  ".join(preview.splitlines()[:8]))
    print("  …")
    print()

    if not sender.enabled:
        print("✗ WhatsAppSender désactivé — voir logs ci-dessus pour la raison.")
        return 2

    print("Envoi en cours…")
    try:
        sent = sender.send(brief)
    except Exception as e:
        print(f"✗ {type(e).__name__}: {e}")
        return 3

    if not sent:
        print("✗ Aucun message accepté — voir logs pour le détail de la réponse Wassoya.")
        return 4

    print(f"✓ Message envoyé à {sent}")
    print("  → Vérifier WhatsApp sur le numéro destinataire.")
    print("  → Si reçu : Wassoya OK, on peut activer l'envoi WhatsApp en prod.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
