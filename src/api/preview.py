"""Prévisualisation de la charte graphique email (sans envoi).

Deux endpoints :
- `GET /preview/brief` : brief d'exemple (fixture locale) — utile pour itérer
  sur le design.
- `GET /preview/brief/{id}` : rend l'email du brief #id stocké en DB.

Ces routes sont **non authentifiées** pour pouvoir être ouvertes dans un
navigateur sans configurer de token. Elles n'exposent que du HTML de
présentation (aucune donnée personnelle, aucun secret).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from ..config import get_settings
from ..database import get_session
from ..delivery.email_brevo import render_email_html
from ..delivery.sample_brief import sample_brief, sample_snapshot
from ..models import Brief

router = APIRouter(prefix="/preview", tags=["preview"])


def _today_fr() -> str:
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    return datetime.now(tz).strftime("%A %d %B %Y").capitalize()


@router.get("/brief", response_class=HTMLResponse)
def preview_sample_brief(
    variant: str = Query(
        "full",
        pattern="^(full|empty|error)$",
        description="full = brief complet, empty = sans opportunité, error = synthèse dégradée",
    ),
):
    """Prévisualise le template email avec un brief d'exemple."""
    if variant == "empty":
        brief = {
            "market_summary": "Marché en consolidation, volumes faibles. Aucun catalyseur identifié.",
            "market_regime": "range",
            "opportunities": [],
            "alerts": ["Résultats ETIT attendus demain"],
            "watchlist_updates": [],
            "skip_reasons": (
                "Pas de configuration claire aujourd'hui. Les volumes restent faibles "
                "et aucune publication matérielle n'est attendue dans les 48h. "
                "Mieux vaut attendre un catalyseur."
            ),
        }
        snapshot = sample_snapshot()
    elif variant == "error":
        brief = {
            "market_summary": "Erreur de génération du brief. Voir logs pour détail.",
            "opportunities": [],
            "alerts": ["Synthèse indisponible : anthropic: 529 Overloaded"],
            "skip_reasons": "anthropic: 529 Overloaded",
            "_error": True,
        }
        snapshot = None
    else:
        brief = sample_brief()
        snapshot = sample_snapshot()

    _subject, html = render_email_html(
        brief,
        _today_fr(),
        market_snapshot=snapshot,
        edition_num="PREVIEW",
    )
    return HTMLResponse(html)


@router.get("/brief/{brief_id}", response_class=HTMLResponse)
def preview_stored_brief(brief_id: int):
    """Rend l'email d'un brief déjà stocké en DB."""
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    with get_session() as s:
        brief = s.get(Brief, brief_id)
        if not brief:
            raise HTTPException(status_code=404, detail="Brief introuvable")
        date_str = brief.brief_date.astimezone(tz).strftime("%A %d %B %Y").capitalize()
        payload = brief.payload or {}

    _subject, html = render_email_html(
        payload,
        date_str,
        market_snapshot=None,
        edition_num=brief_id,
    )
    return HTMLResponse(html)


@router.get("", response_class=HTMLResponse)
def preview_index():
    """Index simple listant les variantes."""
    items = [
        ("/preview/brief?variant=full",  "Brief complet (3 opportunités + snapshot + alertes)"),
        ("/preview/brief?variant=empty", "Brief sans opportunité (honnêteté analyste)"),
        ("/preview/brief?variant=error", "Brief dégradé (synthèse LLM échouée)"),
    ]
    rows = "".join(
        f'<li><a href="{href}" style="color:#0A2540;">{label}</a></li>'
        for href, label in items
    )
    return HTMLResponse(f"""
    <!doctype html>
    <html lang="fr"><head><meta charset="utf-8"><title>BRVM Agent · Preview</title>
    <style>body{{font-family:-apple-system,Helvetica,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#1E293B;}}h1{{font-family:Georgia,serif;color:#0A2540;}}li{{margin:8px 0;}}</style>
    </head><body>
    <h1>BRVM Agent · Aperçu email</h1>
    <p>Prévisualise la charte graphique sans envoyer d'email.</p>
    <ul>{rows}</ul>
    <p><small>Pour un brief réel stocké en DB : <code>/preview/brief/{{id}}</code></small></p>
    </body></html>
    """)
