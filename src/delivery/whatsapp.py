"""Livraison WhatsApp via Wassoya (https://wassoya.com/docs).

Wassoya impose l'envoi via **template Meta approuvé** — pas de texte libre.
Le template doit être créé et validé par Meta via l'interface Wassoya AVANT
tout envoi en prod. Il doit accepter au moins **une variable** pour le
contenu du brief (placeholder `{{1}}`).

Exemple de template Meta à soumettre (côté Wassoya) :
    Name: `brvm_brief_v1`
    Body: `{{1}}`
    Category: MARKETING (ou UTILITY selon l'usage)

Ensuite, définir dans .env :
    WASSOYA_API_KEY=...
    WASSOYA_SENDER_NUMBER=2250700000000     # sans +
    WASSOYA_TEMPLATE_NAME=brvm_brief_v1

Schéma API (résumé) :
    POST {base}/whatsapp/messages
    Authorization: Bearer {api_key}
    Body: {
      "from": "2250700000000",
      "to": "2250711111111",
      "templateName": "brvm_brief_v1",
      "parameters": ["<texte du brief>"]
    }
    Réponse OK : {"success": true, "data": {"id": "msg_...", "status": "SUBMITTED", ...}}
    Réponse NOK : {"success": false, "error": "..."}
"""
from __future__ import annotations

import logging

import httpx

from ..config import get_settings
from .repository import active_recipients

logger = logging.getLogger(__name__)

# Limite défensive — WhatsApp templates tolèrent ~1024 chars par variable,
# mais on coupe plus tôt pour garantir l'acceptation (formatage variable selon template).
_MAX_PARAM_CHARS = 900


def format_brief_short(brief: dict) -> str:
    """Résumé WhatsApp court — injecté comme paramètre {{1}} du template."""
    opps = brief.get("opportunities") or []
    lines = [f"*Brief BRVM* - {len(opps)} opportunité(s)"]
    lines.append(f"_{brief.get('market_summary', '')[:200]}_")
    lines.append("")
    for o in opps[:3]:
        direction = o.get("direction", "?").upper()
        conviction = "⭐" * int(o.get("conviction", 3))
        lines.append(f"*{o.get('ticker', '?')}* — {direction} {conviction}")
        lines.append(f"{o.get('thesis', '')[:150]}")
        lines.append("")
    if brief.get("alerts"):
        lines.append("⚠️ " + " · ".join(brief["alerts"][:3]))
    lines.append("")
    lines.append("_Détail complet : voir email_")
    text = "\n".join(lines)
    return text[:_MAX_PARAM_CHARS]


def _strip_plus(number: str) -> str:
    """Wassoya attend le numéro au format 2250700000000 (sans `+`)."""
    return number.lstrip("+").replace(" ", "")


class WhatsAppSender:
    """Envoie un brief WhatsApp via Wassoya à tous les recipients actifs.

    `enabled` = vrai si :
      - `WASSOYA_API_KEY` défini
      - `WASSOYA_SENDER_NUMBER` défini
      - `WASSOYA_TEMPLATE_NAME` défini
      - Au moins 1 recipient actif (channel='whatsapp')

    Sinon, le pipeline skippe silencieusement en logguant la raison.
    """

    def __init__(self):
        self.s = get_settings()
        self._recipients = active_recipients("whatsapp")
        self.enabled = bool(
            self.s.wassoya_api_key
            and self.s.wassoya_sender_number
            and self.s.wassoya_template_name
            and self._recipients
        )

    def send(self, brief: dict) -> list[str]:
        """Envoie le brief à tous les numéros actifs. Retourne les numéros servis (avec `+`)."""
        if not self.enabled:
            self._log_skip_reason()
            return []

        text = format_brief_short(brief)
        url = f"{self.s.wassoya_api_base_url.rstrip('/')}/whatsapp/messages"
        headers = {
            "Authorization": f"Bearer {self.s.wassoya_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        sender = _strip_plus(self.s.wassoya_sender_number)

        sent: list[str] = []
        for number, _name in self._recipients:
            to = _strip_plus(number)
            payload = {
                "from": sender,
                "to": to,
                "templateName": self.s.wassoya_template_name,
                "parameters": [text],
            }
            try:
                resp = httpx.post(url, headers=headers, json=payload, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success"):
                    logger.error(
                        f"Wassoya a refusé pour {number} : "
                        f"{data.get('error', 'erreur sans message')}"
                    )
                    continue
                msg_id = data.get("data", {}).get("id", "?")
                logger.info(f"Wassoya OK pour {number} — id={msg_id}")
                sent.append(number)
            except httpx.HTTPStatusError as e:
                preview = e.response.text[:300] if e.response is not None else ""
                logger.error(
                    f"Wassoya HTTP {e.response.status_code} pour {number} : {preview}"
                )
            except Exception:
                logger.exception(f"Wassoya exception pour {number}")

        logger.info(f"WhatsApp envoyé à {len(sent)}/{len(self._recipients)} destinataire(s)")
        return sent

    def _log_skip_reason(self) -> None:
        if not self.s.wassoya_api_key:
            logger.info("WhatsApp désactivé (WASSOYA_API_KEY non défini)")
        elif not self.s.wassoya_sender_number:
            logger.info("WhatsApp désactivé (WASSOYA_SENDER_NUMBER non défini)")
        elif not self.s.wassoya_template_name:
            logger.info("WhatsApp désactivé (WASSOYA_TEMPLATE_NAME non défini)")
        elif not self._recipients:
            logger.info("WhatsApp désactivé (aucun recipient channel='whatsapp')")


__all__ = ["WhatsAppSender", "format_brief_short"]
