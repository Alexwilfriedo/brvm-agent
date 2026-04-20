"""Schemas Pydantic du brief quotidien.

Miroir typé du JSON produit par Opus (voir `prompts/synthesis.md`). On tolère
les champs manquants / supplémentaires (`extra="ignore"`) pour ne pas casser
si le modèle ajoute un champ ou en omet un.

Ces schemas servent à deux choses :
1. Valider le payload avant rendu (rejette les briefs malformés tôt).
2. Driver le template Jinja2 avec des attributs au lieu de `dict.get(...)`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Direction = Literal["buy", "watch", "avoid", "hold", "reduce"]
TimeHorizon = Literal["court", "moyen", "long"]
Regime = Literal[
    "trend_up", "trend_down", "range", "risk_off", "event_driven", "illiquid"
]


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    name: str = ""
    direction: Direction = "watch"
    conviction: int = Field(default=3, ge=1, le=5)
    time_horizon: TimeHorizon | None = None
    thesis: str = ""
    signals: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    entry_zone_fcfa: str | None = None
    invalidation: str | None = None


class BriefPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market_summary: str = ""
    market_regime: Regime | None = None
    opportunities: list[Opportunity] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    watchlist_updates: list[str] = Field(default_factory=list)
    skip_reasons: str = ""

    # Flag interne quand la synthèse a échoué (voir synthesis.py::_error_payload)
    is_error: bool = Field(default=False, alias="_error")
    error_preview: str = Field(default="", alias="_raw_preview")

    @classmethod
    def from_raw(cls, raw: dict | None) -> BriefPayload:
        """Parse un dict brut en tolérant un payload incomplet."""
        if not raw:
            return cls()
        return cls.model_validate(raw)
