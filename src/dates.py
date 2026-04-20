"""Formatage de dates en français, indépendant de la locale système.

Railway/Nixpacks ne ship pas `fr_FR.UTF-8` par défaut — on ne peut pas compter
sur `locale.setlocale(LC_TIME, 'fr_FR.UTF-8')`. On utilise une table de
mapping explicite qui fonctionne partout.
"""
from __future__ import annotations

from datetime import datetime

_JOURS = [
    "Lundi", "Mardi", "Mercredi", "Jeudi",
    "Vendredi", "Samedi", "Dimanche",
]

_MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def format_date_fr(dt: datetime) -> str:
    """Formate une date au format "Lundi 21 avril 2026" sans dépendre de la locale."""
    return f"{_JOURS[dt.weekday()]} {dt.day} {_MOIS[dt.month - 1]} {dt.year}"
