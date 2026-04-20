"""Valide Sentry en envoyant une exception de test + un message info.

Usage :
    .venv/bin/python scripts/check_sentry.py
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        print("✗ SENTRY_DSN manquant dans .env.")
        print("  → Créer un projet 'FastAPI' sur sentry.io, copier le DSN (https://xxx@oyyy.ingest.sentry.io/zzz).")
        return 1

    if not dsn.startswith("https://"):
        print(f"✗ DSN invalide (doit commencer par https://) : {dsn[:40]}…")
        return 1

    print(f"✓ DSN détecté ({dsn[:32]}…)")
    print("  Init Sentry…")

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "dev"),
        traces_sample_rate=1.0,  # on force pour le test
        release="brvm-agent@check-sentry",
    )

    # 1. Message info
    sentry_sdk.capture_message(
        "BRVM Agent · test d'envoi Sentry (info)",
        level="info",
    )
    print("  Message 'info' envoyé.")

    # 2. Breadcrumbs + exception
    sentry_sdk.add_breadcrumb(category="test", message="étape 1", level="info")
    sentry_sdk.add_breadcrumb(category="test", message="étape 2 — avant erreur", level="info")
    try:
        raise RuntimeError("BRVM Agent · test d'envoi Sentry (exception)")
    except RuntimeError:
        sentry_sdk.capture_exception()
    print("  Exception 'RuntimeError' envoyée.")

    # Flush avant de sortir (sinon on peut perdre les events si le process meurt vite)
    print("  Flush en cours…")
    client = sentry_sdk.get_client()
    if client is not None:
        client.flush(timeout=5.0)
    time.sleep(1.0)

    print()
    print("OK — 2 events envoyés à Sentry.")
    print("  → Ouvrir ton dashboard Sentry → Issues.")
    print("  → Tu devrais voir 1 message 'info' + 1 issue 'RuntimeError: BRVM Agent · test d'envoi'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
