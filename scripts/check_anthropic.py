"""Valide que la clé Anthropic fonctionne et qu'on a du crédit.

Fait 2 appels minimaux :
- Sonnet (modèle d'enrichissement)
- Opus  (modèle de synthèse)

Affiche la latence + le coût approx. Usage :
    .venv/bin/python scripts/check_anthropic.py
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()


def _check(model: str, label: str) -> tuple[bool, float, str]:
    from anthropic import Anthropic, APIError

    client = Anthropic()  # lit ANTHROPIC_API_KEY depuis l'env
    t0 = time.perf_counter()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=30,
            messages=[{"role": "user", "content": "Réponds en un mot: pong"}],
        )
    except APIError as e:
        return False, 0.0, f"{type(e).__name__}: {e}"
    dt = (time.perf_counter() - t0) * 1000
    text = resp.content[0].text.strip() if resp.content else "(vide)"
    usage = getattr(resp, "usage", None)
    suffix = ""
    if usage:
        suffix = f"  [in={usage.input_tokens} out={usage.output_tokens}]"
    return True, dt, f"{text}{suffix}"


def main() -> int:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key or key == "sk-ant-xxxxx" or not key.startswith("sk-ant-"):
        print("✗ ANTHROPIC_API_KEY manquant ou placeholder. Colle la clé dans .env.")
        return 1
    print(f"✓ Clé détectée ({key[:12]}…{key[-4:]})")

    ok_all = True
    for model, label in [
        (os.getenv("MODEL_ENRICHMENT", "claude-sonnet-4-6"), "Sonnet (enrichissement)"),
        (os.getenv("MODEL_SYNTHESIS",  "claude-opus-4-7"),   "Opus    (synthèse)"),
    ]:
        ok, dt, info = _check(model, label)
        mark = "✓" if ok else "✗"
        print(f"{mark} {label:28s}  model={model:22s}  latency={dt:>6.0f}ms  {info}")
        ok_all &= ok

    if ok_all:
        print("\nOK — la clé Anthropic est fonctionnelle, on peut enchaîner sur Brevo.")
        return 0
    print("\nÉchec — vérifier crédit sur console.anthropic.com/settings/billing.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
