"""Livraison WhatsApp via l'API Brevo WhatsApp Business.

Docs Brevo : https://developers.brevo.com/reference/sendwhatsappmessage

Deux modes possibles :
  1. Template Meta pré-approuvé (obligatoire hors fenêtre 24h) — RECOMMANDÉ
  2. Message libre (seulement si conversation active < 24h)

Pour un brief matinal automatique, utilise un template approuvé par Meta.
"""
import logging

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

BREVO_WHATSAPP_URL = "https://api.brevo.com/v3/whatsapp/sendMessage"


def format_brief_short(brief: dict) -> str:
    """Résumé WhatsApp très court (< 1000 chars) — complément de l'email."""
    opps = brief.get("opportunities") or []
    lines = [f"*Brief BRVM* - {len(opps)} opportunité(s)"]
    lines.append(f"_{brief.get('market_summary','')[:200]}_")
    lines.append("")
    for o in opps[:3]:
        direction = o.get("direction", "?").upper()
        conviction = "⭐" * int(o.get("conviction", 3))
        lines.append(f"*{o.get('ticker','?')}* — {direction} {conviction}")
        lines.append(f"{o.get('thesis','')[:150]}")
        lines.append("")
    if brief.get("alerts"):
        lines.append("⚠️ " + " · ".join(brief["alerts"][:3]))
    lines.append("")
    lines.append("_Détail complet : voir email_")
    return "\n".join(lines)


class WhatsAppSender:
    def __init__(self):
        self.s = get_settings()
        self.enabled = bool(self.s.brevo_api_key and self.s.whatsapp_to_number)

    def send(self, brief: dict) -> None:
        """Envoie un message WhatsApp. Utilise le template si configuré, sinon message libre."""
        if not self.enabled:
            logger.warning("WhatsApp désactivé (BREVO_API_KEY ou WHATSAPP_TO_NUMBER manquant)")
            return

        text = format_brief_short(brief)

        headers = {
            "api-key": self.s.brevo_api_key,
            "content-type": "application/json",
            "accept": "application/json",
        }

        if self.s.whatsapp_template_id:
            # Mode template (recommandé pour messages sortants automatiques)
            payload = {
                "senderNumber": self.s.whatsapp_sender_number,
                "contactNumbers": [self.s.whatsapp_to_number],
                "templateId": int(self.s.whatsapp_template_id),
                # Les paramètres dépendent de ton template approuvé
                "params": {"brief_text": text[:1000]},
            }
        else:
            # Mode texte libre (seulement si l'utilisateur a écrit dans les 24h)
            payload = {
                "senderNumber": self.s.whatsapp_sender_number,
                "contactNumbers": [self.s.whatsapp_to_number],
                "text": text,
            }

        try:
            resp = httpx.post(BREVO_WHATSAPP_URL, headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            logger.info(f"WhatsApp envoyé à {self.s.whatsapp_to_number}")
        except httpx.HTTPStatusError as e:
            logger.error(f"WhatsApp Brevo erreur {e.response.status_code}: {e.response.text}")
            raise
