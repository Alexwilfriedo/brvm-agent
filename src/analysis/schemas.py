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


class Valuation(BaseModel):
    """Ratios fondamentaux optionnels, alignés avec la grille des notes
    bimensuelles des bureaux sell-side ivoiriens (J&D Advisory, CGF Bourse…).

    Tous optionnels : Opus remplit ce qu'il peut inférer des fondamentaux
    Sika passés en contexte + estimations explicites. Une valeur absente
    signifie "non communiqué / non estimable".
    """
    model_config = ConfigDict(extra="ignore")

    # Dividende par action — ajusté split éventuel
    dpa_current: float | None = None
    dpa_estimate: float | None = None
    # Price / Book
    p_b_current: float | None = None
    p_b_estimate: float | None = None
    # Price / Earnings Ratio
    per_current: float | None = None
    per_estimate: float | None = None
    # Rendement dividende (%)
    dividend_yield_current: float | None = None
    dividend_yield_estimate: float | None = None


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    name: str = ""
    sector: str = ""
    direction: Direction = "watch"
    conviction: int = Field(default=3, ge=1, le=5)
    time_horizon: TimeHorizon | None = None
    thesis: str = ""
    signals: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    # Prix & potentiel (format JDA Advisory)
    price_current: float | None = None     # cours du jour, FCFA
    price_target: float | None = None      # cours cible, FCFA
    gain_potential_pct: float | None = None  # (target - current) / current × 100
    price_range_min: float | None = None   # prix min du range d'entrée
    price_range_max: float | None = None   # prix max du range d'entrée

    # Ratios fondamentaux
    valuation: Valuation | None = None

    # Conservé pour rétro-compat et usage libre — à terme on privilégie les
    # champs chiffrés ci-dessus.
    entry_zone_fcfa: str | None = None
    invalidation: str | None = None


def _coerce_none_to_empty(v: object) -> str:
    """Opus peut renvoyer null pour une string vide → on coerce en ''."""
    return "" if v is None else v  # type: ignore[return-value]


class BriefPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Opus renvoie parfois null pour les strings "vides". On tolère.
    market_summary: str = Field(default="")
    market_regime: Regime | None = None
    opportunities: list[Opportunity] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    watchlist_updates: list[str] = Field(default_factory=list)
    skip_reasons: str = Field(default="")

    @classmethod
    def _strip_none_strings(cls, v: dict) -> dict:
        """Coerce les string fields à '' si Opus les a retournés à null."""
        if not isinstance(v, dict):
            return v
        for key in ("market_summary", "skip_reasons"):
            if v.get(key) is None:
                v[key] = ""
        return v

    # Flag interne quand la synthèse a échoué (voir synthesis.py::_error_payload)
    is_error: bool = Field(default=False, alias="_error")
    error_preview: str = Field(default="", alias="_raw_preview")

    @classmethod
    def from_raw(cls, raw: dict | None) -> BriefPayload:
        """Parse un dict brut en tolérant un payload incomplet.

        Opus renvoie parfois null pour les champs string "vides" (ex.
        `skip_reasons: null` quand il a des opportunités). On sanitize avant
        validation pour éviter un crash qui casserait la livraison.
        """
        if not raw:
            return cls()
        sanitized = cls._strip_none_strings(dict(raw))
        return cls.model_validate(sanitized)
