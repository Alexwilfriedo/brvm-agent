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
                "entry_zone_fcfa": "6 450 - 6 650",
                "invalidation": "Rupture sous 6 200 FCFA ou guidance baissière T2 matérielle.",
            },
            {
                "ticker": "SNTS",
                "name": "Sonatel",
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
                "invalidation": "Confirmation d'un plafonnement agressif des frais MM > 30% d'impact EBITDA.",
            },
            {
                "ticker": "PALC",
                "name": "PALMCI",
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
