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


# =============================================================================
# Brief HEBDOMADAIRE — audit 7j + scorecard P&L réel
# =============================================================================

Outcome = Literal["won", "lost", "pending"]


class Play(BaseModel):
    """Un signal émis pendant la semaine, enrichi de son P&L réel.

    Contrairement à `Opportunity` (daily, forward-looking), `Play` est
    backward-looking : on sait déjà ce qu'il a rendu entre `price_at_signal`
    et le cours de clôture de la semaine.
    """
    model_config = ConfigDict(extra="ignore")

    ticker: str
    name: str = ""
    sector: str = ""
    direction: Direction = "watch"
    conviction: int = Field(default=3, ge=1, le=5)
    # Date du brief daily qui a émis ce signal (YYYY-MM-DD en ISO)
    issued_on: str = ""
    price_at_signal: float | None = None
    current_price: float | None = None
    # P&L réalisé en % (signe inversé pour direction='avoid' — un -5% sur un avoid = gain)
    realized_pnl_pct: float | None = None
    outcome: Outcome = "pending"
    # Leçon apprise si le call a raté (Opus l'écrit en post-analyse)
    lesson: str = ""
    # Thèse originale, copiée depuis le brief daily (contexte de lecture)
    thesis: str = ""


class WeeklyScorecard(BaseModel):
    """Statistiques agrégées de la semaine."""
    model_config = ConfigDict(extra="ignore")

    total_calls: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    # P&L moyen réalisé (%) sur les calls clos (won+lost) — signe corrigé pour avoid
    avg_realized_pnl_pct: float | None = None
    # Meilleur et pire call (pour mise en avant)
    best_ticker: str | None = None
    best_pnl_pct: float | None = None
    worst_ticker: str | None = None
    worst_pnl_pct: float | None = None


class TradeExecution(BaseModel):
    """Observation sur les trades utilisateur de la semaine.

    Pose un miroir comportemental : qu'est-ce que l'utilisateur a réellement
    fait vs ce qu'on a recommandé ? Sert à l'audit, pas au jugement.
    """
    model_config = ConfigDict(extra="ignore")

    total_trades: int = 0
    following_signal: int = 0    # trades avec reason='brief' ou signal_id lié
    autonomous: int = 0          # trades 'intuition' / 'news' / 'other'
    avg_unrealized_pnl_pct: float | None = None
    # Observation narrative rédigée par Opus (1-2 phrases sobres)
    commentary: str = ""


class WeeklyBriefPayload(BaseModel):
    """Brief hebdomadaire : audit des recos + outlook semaine suivante.

    Philosophiquement différent du daily : pas de nouveaux signaux, uniquement
    une revue de performance + preview des catalyseurs à venir. Le lecteur type
    est un expert/sponsor qui audite la qualité du système, pas un trader qui
    agit dessus.
    """
    model_config = ConfigDict(extra="ignore")

    # Fenêtre couverte (ISO YYYY-MM-DD)
    week_start: str = ""
    week_end: str = ""
    # Régime de marché dominant sur la semaine
    market_regime: Regime | None = None
    # Résumé narratif de la semaine (marché global, rotation sectorielle…)
    week_summary: str = Field(default="")
    # Scorecard agrégé
    scorecard: WeeklyScorecard = Field(default_factory=WeeklyScorecard)
    # Observation sur les trades utilisateur de la semaine (si déclarés)
    trade_execution: TradeExecution = Field(default_factory=TradeExecution)
    # Calls émis, triés par conviction/P&L côté prompt
    plays: list[Play] = Field(default_factory=list)
    # News structurelles (réglementation, résultats majeurs, M&A) — pas le bruit quotidien
    structural_news: list[str] = Field(default_factory=list)
    # Catalyseurs connus pour la semaine à venir (ex-dates, publications, AG)
    week_ahead_catalysts: list[str] = Field(default_factory=list)
    # Watchlist updates (titres sortis, entrés, commentaires)
    watchlist_updates: list[str] = Field(default_factory=list)

    # Flag interne si la synthèse a échoué
    is_error: bool = Field(default=False, alias="_error")
    error_preview: str = Field(default="", alias="_raw_preview")

    @classmethod
    def _strip_none_strings(cls, v: dict) -> dict:
        """Coerce les string fields à '' si Opus les a retournés à null."""
        if not isinstance(v, dict):
            return v
        for key in ("week_summary", "week_start", "week_end"):
            if v.get(key) is None:
                v[key] = ""
        return v

    @classmethod
    def from_raw(cls, raw: dict | None) -> WeeklyBriefPayload:
        if not raw:
            return cls()
        sanitized = cls._strip_none_strings(dict(raw))
        return cls.model_validate(sanitized)
