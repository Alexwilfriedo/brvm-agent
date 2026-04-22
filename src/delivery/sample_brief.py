"""Brief d'exemple pour prévisualiser la charte graphique.

Utilisé par `GET /api/briefs/preview`. Couvre l'essentiel de la charte :
- 3 opportunités (buy, watch, avoid) avec convictions variées
- Catalysts, risks, signals, invalidation, entry_zone_fcfa
- Alerts + watchlist_updates
- Top gainers/losers dans le snapshot
- Régime de marché
"""
from __future__ import annotations


def sample_brief() -> dict:
    return {
        "market_summary": (
            "BRVM Composite +0,42% à 256,18 pts, portée par le secteur bancaire "
            "(BOAC +2,1%, SGBC +1,6%) dans un volume modéré (1,8 Md FCFA). "
            "Tensions sur les télécoms après l'annonce ARTCI sur la tarification mobile money."
        ),
        "market_regime": "range",
        "opportunities": [
            {
                "ticker": "BOAC",
                "name": "Bank of Africa Côte d'Ivoire",
                "sector": "Banques",
                "direction": "buy",
                "conviction": 4,
                "time_horizon": "moyen",
                "thesis": (
                    "Publication T1 2026 en avance sur le consensus : PNB +11% a/a, "
                    "coût du risque contenu à 0,9%. La décote vs. SGBC reste de ~18% "
                    "alors que le ROE converge. Dividende 2025 (450 FCFA) déjà acquis, "
                    "rendement brut 6,8% sur la base actuelle."
                ),
                "signals": [
                    "PNB T1 +11% vs +6% consensus",
                    "Volume 3 dernières séances 2,3x MA20",
                    "Ratio cost/income 48% — meilleur de la cote",
                ],
                "catalysts": [
                    "AG ordinaire 22/05 — vote dividende",
                    "Publication semestrielle attendue fin juillet",
                ],
                "risks": [
                    "Exposition souverain Mali (~8% bilan)",
                    "Corrélation forte avec taux BCEAO",
                ],
                "price_current": 6600,
                "price_target": 7500,
                "gain_potential_pct": 13.64,
                "price_range_min": 6450,
                "price_range_max": 6650,
                "valuation": {
                    "dpa_current": 450.0,
                    "dpa_estimate": 500.0,
                    "p_b_current": 1.4,
                    "p_b_estimate": 1.3,
                    "per_current": 6.2,
                    "per_estimate": 5.8,
                    "dividend_yield_current": 6.82,
                    "dividend_yield_estimate": 7.58,
                },
                "invalidation": "Rupture sous 6 200 FCFA ou guidance baissière T2 matérielle.",
            },
            {
                "ticker": "SNTS",
                "name": "Sonatel",
                "sector": "Télécoms",
                "direction": "watch",
                "conviction": 3,
                "time_horizon": "court",
                "thesis": (
                    "La décision ARTCI sur la tarification mobile money crée une asymétrie "
                    "négative court terme mais ouvre une fenêtre d'achat si la correction "
                    "dépasse -5%. Fondamentaux long terme intacts (leadership Orange Money)."
                ),
                "signals": [
                    "Volume accru sur la cassure technique 19 500 FCFA",
                    "Flux vendeurs institutionnels sur 2 séances",
                ],
                "catalysts": [
                    "Clarification ARTCI attendue sous 10 jours",
                    "Résultats T1 début mai",
                ],
                "risks": [
                    "Impact régulatoire non chiffré à ce stade",
                    "Pression sur ARPU mobile money",
                ],
                "price_current": 19250,
                "price_target": 20500,
                "gain_potential_pct": 6.49,
                "price_range_min": 18500,
                "price_range_max": 19000,
                "valuation": {
                    "dpa_current": 1590.0,
                    "dpa_estimate": 1650.0,
                    "p_b_current": 3.1,
                    "p_b_estimate": 2.9,
                    "per_current": 10.8,
                    "per_estimate": 10.2,
                    "dividend_yield_current": 8.26,
                    "dividend_yield_estimate": 8.57,
                },
                "invalidation": "Confirmation d'un plafonnement agressif des frais MM > 30% d'impact EBITDA.",
            },
            {
                "ticker": "PALC",
                "name": "PALMCI",
                "sector": "Agro-industrie",
                "direction": "avoid",
                "conviction": 2,
                "time_horizon": "moyen",
                "thesis": (
                    "Rally récent sur anticipation d'une hausse du cours de la palme est "
                    "découplé des fondamentaux. Le cours CPO stagne en réalité. Risque de "
                    "normalisation post ex-date dividende."
                ),
                "signals": [
                    "Prix CPO Kuala Lumpur stable sur 4 semaines",
                    "Ex-date dividende 12/05 — effet technique attendu",
                ],
                "catalysts": [],
                "risks": [
                    "Spread bid/ask ~3% (liquidité réduite)",
                    "Pluviométrie 2026 inférieure à la norme",
                ],
                "price_current": 14200,
                "price_target": 12500,
                "gain_potential_pct": -11.97,
                "valuation": {
                    "per_current": 14.5,
                    "per_estimate": 15.2,
                    "dividend_yield_current": 4.93,
                    "dividend_yield_estimate": 4.5,
                },
                "invalidation": "Signal haussier durable CPO > 4 500 MYR avec suivi volumique.",
            },
        ],
        "alerts": [
            "Ex-date dividende BOAC le 22/05 (450 FCFA)",
            "Publication résultats ETIT attendue cette semaine",
            "Décision BCEAO sur taux directeur fin mai",
            "Calendrier AG bancaires CI chargé (4 émetteurs en juin)",
        ],
        "watchlist_updates": [
            "SPHC : volume anormal 3 séances, accumulation probable — surveiller confirmation",
            "SGBC : franchissement résistance 14 200 FCFA, retest à guetter",
        ],
        "skip_reasons": "",
    }


def sample_weekly_brief() -> dict:
    """Fixture pour prévisualiser le template hebdomadaire.

    Démontre les 3 outcomes (won/lost/pending) + une leçon sur le call raté
    + mix de directions (buy/avoid/watch) pour couvrir tous les cas visuels.
    """
    return {
        "week_start": "2026-04-13",
        "week_end": "2026-04-17",
        "market_regime": "trend_up",
        "week_summary": (
            "Semaine haussière pour le Composite BRVM (+1,24%), portée par les "
            "banques après des résultats T1 globalement au-dessus du consensus "
            "(BOAC +4,3%, SGBC +2,8%). Rotation sectorielle claire des télécoms "
            "vers les financières — la décision ARTCI sur la tarification mobile "
            "money a pesé sur SNTS et ONTBF. Volumes en expansion modérée "
            "(1,9 Md FCFA/j vs 1,6 Md la semaine précédente)."
        ),
        "scorecard": {
            "total_calls": 8,
            "wins": 5,
            "losses": 2,
            "pending": 1,
            "avg_realized_pnl_pct": 1.87,
            "best_ticker": "BOAC",
            "best_pnl_pct": 4.31,
            "worst_ticker": "PALC",
            "worst_pnl_pct": -3.12,
        },
        "plays": [
            {
                "ticker": "BOAC",
                "name": "Bank of Africa Côte d'Ivoire",
                "sector": "Banques",
                "direction": "buy",
                "conviction": 4,
                "issued_on": "2026-04-14",
                "price_at_signal": 6500,
                "current_price": 6780,
                "realized_pnl_pct": 4.31,
                "outcome": "won",
                "lesson": "",
                "thesis": "Publication T1 > consensus (PNB +11%), ROE en hausse. Thèse confirmée par le flux institutionnel.",
                "days_held": 3,
            },
            {
                "ticker": "SGBC",
                "name": "Société Générale CI",
                "sector": "Banques",
                "direction": "buy",
                "conviction": 3,
                "issued_on": "2026-04-15",
                "price_at_signal": 14050,
                "current_price": 14450,
                "realized_pnl_pct": 2.85,
                "outcome": "won",
                "lesson": "",
                "thesis": "Breakout technique au-dessus de 14 000 avec volume 1,8x MA20.",
                "days_held": 2,
            },
            {
                "ticker": "PALC",
                "name": "PALMCI",
                "sector": "Agro-industrie",
                "direction": "avoid",
                "conviction": 2,
                "issued_on": "2026-04-15",
                "price_at_signal": 14200,
                "current_price": 14640,
                "realized_pnl_pct": -3.10,
                "outcome": "lost",
                "lesson": (
                    "Avoid émis sans catalyseur négatif concret — la thèse de "
                    "normalisation post ex-date a été ignorée par le marché. "
                    "Sous-estimation du soutien technique à 14 000."
                ),
                "thesis": "Rally sans fondamentaux (CPO stable), normalisation attendue post ex-date.",
                "days_held": 2,
            },
            {
                "ticker": "SNTS",
                "name": "Sonatel",
                "sector": "Télécoms",
                "direction": "watch",
                "conviction": 3,
                "issued_on": "2026-04-16",
                "price_at_signal": 19250,
                "current_price": 19100,
                "realized_pnl_pct": -0.78,
                "outcome": "pending",
                "lesson": "",
                "thesis": "Impact ARTCI à observer. Reprise possible sous 18 500.",
                "days_held": 1,
            },
        ],
        "structural_news": [
            "BCEAO maintient taux directeur à 3,50% (décision 15/04)",
            "SNTS publie T1 : ARPU stable malgré ARTCI",
            "BOAC distribue dividende 450 FCFA le 22/05",
        ],
        "week_ahead_catalysts": [
            "BOAC AG ordinaire mardi 22/04 — vote dividende",
            "ETIT publication résultats attendue vendredi",
            "Ex-date SGBC jeudi (DPS 1 050 FCFA)",
        ],
        "watchlist_updates": [
            "SPHC entre en watchlist (breakout 4 200 FCFA, volume 2× MA20)",
            "UNLC sort — volumes structurellement faibles",
        ],
    }


def sample_snapshot() -> dict:
    return {
        "date": "2026-04-18",
        "quotes_count": 42,
        "top_gainers": [
            {"ticker": "BOAC", "name": "Bank of Africa CI", "var_pct": 2.10, "volume": 12500},
            {"ticker": "SGBC", "name": "Société Générale CI", "var_pct": 1.64, "volume": 3200},
            {"ticker": "SPHC", "name": "SAPH", "var_pct": 1.45, "volume": 8700},
            {"ticker": "NSBC", "name": "NSIA Banque", "var_pct": 1.10, "volume": 4100},
            {"ticker": "CFAC", "name": "CFAO Motors", "var_pct": 0.92, "volume": 1800},
        ],
        "top_losers": [
            {"ticker": "SNTS", "name": "Sonatel", "var_pct": -1.85, "volume": 22000},
            {"ticker": "ONTBF", "name": "Onatel BF", "var_pct": -1.32, "volume": 5400},
            {"ticker": "PALC", "name": "PALMCI", "var_pct": -0.95, "volume": 2900},
            {"ticker": "NTLC", "name": "Nestlé CI", "var_pct": -0.74, "volume": 600},
            {"ticker": "UNLC", "name": "Unilever CI", "var_pct": -0.51, "volume": 1200},
        ],
    }
