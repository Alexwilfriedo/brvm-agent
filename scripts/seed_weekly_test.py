"""Seed une semaine de données fictives pour tester le pipeline weekly.

Crée un état cohérent sur la dernière semaine de trading (lundi → vendredi
le plus récent) :
    - Quotes : 1 par jour pour 4 tickers (BOAC, SGBC, SNTS, PALC)
    - Briefs daily : 3 briefs avec 2-3 signals chacun, price_at_signal réaliste
    - Trades : 2 trades utilisateur pour tester la section trade_execution

Après exécution :
    curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" \\
        http://localhost:8000/api/schedule/run-weekly-now

Le pipeline weekly trouvera les briefs et calculera un vrai P&L realisé
en comparant price_at_signal aux derniers closes.

Usage :
    python -m scripts.seed_weekly_test       # seed normal
    python -m scripts.seed_weekly_test --purge  # purge avant de seed

⚠ Destructif avec --purge : supprime TOUS les briefs/quotes/trades de la
fenêtre ciblée. À n'utiliser qu'en dev local.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from src.config import get_settings
from src.database import get_session, init_db
from src.models import Brief, Quote, Signal, Trade


# --- Configuration du seed --------------------------------------------------

# Tickers réalistes BRVM avec secteur + nom pour que le prompt puisse parler
TICKERS = [
    {"ticker": "BOAC", "name": "Bank of Africa Côte d'Ivoire", "sector": "Banques"},
    {"ticker": "SGBC", "name": "Société Générale CI",          "sector": "Banques"},
    {"ticker": "SNTS", "name": "Sonatel",                      "sector": "Télécoms"},
    {"ticker": "PALC", "name": "PALMCI",                       "sector": "Agro-industrie"},
]

# Évolution fictive des closes sur la semaine (lundi → vendredi)
# Chaque entrée = liste de 5 closes pour (lun, mar, mer, jeu, ven)
PRICE_SERIES = {
    "BOAC": [6500, 6550, 6620, 6720, 6780],  # +4,3% → call buy won
    "SGBC": [14050, 14120, 14200, 14320, 14450],  # +2,8% → call buy won
    "SNTS": [19500, 19300, 19100, 19200, 19100],  # -2,0% → call avoid won
    "PALC": [14200, 14350, 14500, 14600, 14640],  # +3,1% → call avoid lost
}


def _most_recent_friday(ref: datetime) -> datetime:
    days_since_friday = (ref.weekday() - 4) % 7
    return (ref - timedelta(days=days_since_friday)).replace(
        hour=15, minute=30, second=0, microsecond=0,
    )


def _trading_dates(week_end_friday: datetime) -> list[datetime]:
    """Retourne les 5 jours de trading de la semaine se terminant le vendredi donné."""
    week_start = week_end_friday - timedelta(days=4)
    return [week_start + timedelta(days=i) for i in range(5)]


def _purge_window(session, week_start: datetime, week_end: datetime) -> None:
    """Supprime tout ce qui est dans la fenêtre ET toutes les quotes des
    tickers seedés hors fenêtre.

    La purge hors fenêtre est nécessaire : `latest_close_by_ticker` dans le
    pipeline weekly utilise la quote la plus récente TOUS tickers confondus,
    pas restreinte à la fenêtre. Si on laisse des quotes postérieures en DB,
    elles polluent le P&L calculé (cours actuel ≠ close seedé vendredi).
    """
    upper = week_end + timedelta(days=1)
    seeded_tickers = [t["ticker"] for t in TICKERS]

    session.execute(
        delete(Trade)
        .where(Trade.executed_at >= week_start)
        .where(Trade.executed_at < upper)
    )
    # Brief CASCADE supprime les Signals
    session.execute(
        delete(Brief)
        .where(Brief.brief_date >= week_start)
        .where(Brief.brief_date < upper)
    )
    # Purge *toutes* les quotes des tickers seedés (fenêtre + hors fenêtre)
    # pour que le close le plus récent après seed soit vraiment celui de vendredi.
    session.execute(
        delete(Quote)
        .where(Quote.ticker.in_(seeded_tickers))
    )


def _seed_quotes(session, trading_dates: list[datetime]) -> None:
    """5 jours × 4 tickers = 20 quotes."""
    for i, day in enumerate(trading_dates):
        for spec in TICKERS:
            ticker = spec["ticker"]
            close = PRICE_SERIES[ticker][i]
            prev_close = PRICE_SERIES[ticker][i - 1] if i > 0 else close
            var_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

            # Quote normalisée à 15h30 Abidjan (heure de clôture BRVM)
            quote_date = day.replace(hour=15, minute=30, second=0, microsecond=0)
            session.add(Quote(
                ticker=ticker,
                name=spec["name"],
                sector=spec["sector"],
                country="CI",
                close_price=float(close),
                variation_pct=float(var_pct),
                volume=int(10_000 + 2_000 * i),
                quote_date=quote_date,
                extras={
                    "previous_close": float(prev_close),
                    "high": float(close * 1.015),
                    "low": float(close * 0.985),
                    "per": 11.2 if ticker in ("BOAC", "SGBC") else 10.8,
                    "dividend": 450.0 if ticker == "BOAC" else 1050.0,
                    "dividend_yield_pct": 6.8 if ticker == "BOAC" else 7.5,
                    "rsi": 55.0,
                },
            ))


def _seed_briefs(session, trading_dates: list[datetime]) -> list[Brief]:
    """3 briefs daily : lundi, mercredi, vendredi.

    Chaque brief a 2-3 opportunities + signals associés avec price_at_signal.
    La semaine complète génère donc 7 signals.
    """
    briefs_out: list[Brief] = []

    # --- Lundi : call buy BOAC + call avoid SNTS ---
    monday = trading_dates[0].replace(hour=8, minute=0, second=0, microsecond=0)
    b_mon = Brief(
        brief_date=monday,
        brief_type="daily",
        summary_markdown="Séance d'ouverture haussière sur les banques. BOAC publie T1 en avance sur consensus.",
        payload={
            "market_summary": "Séance d'ouverture haussière sur les banques après T1 BOAC. Télécoms sous pression ARTCI.",
            "market_regime": "trend_up",
            "opportunities": [
                {
                    "ticker": "BOAC", "name": "Bank of Africa CI", "sector": "Banques",
                    "direction": "buy", "conviction": 4, "time_horizon": "moyen",
                    "thesis": "Publication T1 2026 supérieure au consensus : PNB +11%, coût du risque maîtrisé. Thèse sectorielle banques ivoiriennes renforcée.",
                    "signals": ["PNB T1 +11%", "Volume 2,3× MA20"],
                    "price_current": PRICE_SERIES["BOAC"][0],
                    "price_target": 7200,
                    "gain_potential_pct": 10.77,
                },
                {
                    "ticker": "SNTS", "name": "Sonatel", "sector": "Télécoms",
                    "direction": "avoid", "conviction": 3, "time_horizon": "court",
                    "thesis": "Décision ARTCI tarification mobile money pèse sur ARPU. Pression sur le cours attendue.",
                    "signals": ["Flux vendeurs institutionnels"],
                    "price_current": PRICE_SERIES["SNTS"][0],
                    "price_target": 18500,
                    "gain_potential_pct": -5.13,
                },
            ],
            "alerts": [], "watchlist_updates": [], "skip_reasons": "",
        },
        revision=1,
        delivery_status="delivered",
    )
    session.add(b_mon)
    session.flush()
    session.add_all([
        Signal(brief_id=b_mon.id, ticker="BOAC", direction="buy",
               conviction=4, thesis="PNB T1 +11%",
               price_at_signal=float(PRICE_SERIES["BOAC"][0]), signal_date=monday),
        Signal(brief_id=b_mon.id, ticker="SNTS", direction="avoid",
               conviction=3, thesis="Pression ARTCI",
               price_at_signal=float(PRICE_SERIES["SNTS"][0]), signal_date=monday),
    ])
    briefs_out.append(b_mon)

    # --- Mercredi : call buy SGBC + call avoid PALC ---
    wednesday = trading_dates[2].replace(hour=8, minute=0, second=0, microsecond=0)
    b_wed = Brief(
        brief_date=wednesday,
        brief_type="daily",
        summary_markdown="Confirmation de la rotation vers les banques. SGBC breakout technique.",
        payload={
            "market_summary": "Confirmation de la rotation défensive vers les banques. SGBC casse sa résistance à 14 200. CPO stable → thèse PALC en décote injustifiée.",
            "market_regime": "trend_up",
            "opportunities": [
                {
                    "ticker": "SGBC", "name": "Société Générale CI", "sector": "Banques",
                    "direction": "buy", "conviction": 3, "time_horizon": "moyen",
                    "thesis": "Breakout technique 14 200 FCFA avec volume confirmé. Rotation sectorielle porteuse. Dividende proche.",
                    "signals": ["Breakout 14 200", "Ex-date jeudi prochaine"],
                    "price_current": PRICE_SERIES["SGBC"][2],
                    "price_target": 15000,
                    "gain_potential_pct": 5.63,
                },
                {
                    "ticker": "PALC", "name": "PALMCI", "sector": "Agro-industrie",
                    "direction": "avoid", "conviction": 2, "time_horizon": "moyen",
                    "thesis": "Rally découplé des fondamentaux. CPO Kuala Lumpur stable. Normalisation post ex-date attendue.",
                    "signals": ["CPO stable 4 semaines"],
                    "price_current": PRICE_SERIES["PALC"][2],
                    "price_target": 13000,
                    "gain_potential_pct": -10.34,
                },
            ],
            "alerts": [], "watchlist_updates": [], "skip_reasons": "",
        },
        revision=1,
        delivery_status="delivered",
    )
    session.add(b_wed)
    session.flush()
    session.add_all([
        Signal(brief_id=b_wed.id, ticker="SGBC", direction="buy",
               conviction=3, thesis="Breakout 14 200",
               price_at_signal=float(PRICE_SERIES["SGBC"][2]), signal_date=wednesday),
        Signal(brief_id=b_wed.id, ticker="PALC", direction="avoid",
               conviction=2, thesis="Rally découplé fondamentaux",
               price_at_signal=float(PRICE_SERIES["PALC"][2]), signal_date=wednesday),
    ])
    briefs_out.append(b_wed)

    # --- Vendredi : watch SNTS (pending — horizon court trop récent) ---
    friday = trading_dates[4].replace(hour=8, minute=0, second=0, microsecond=0)
    b_fri = Brief(
        brief_date=friday,
        brief_type="daily",
        summary_markdown="Clôture hebdo. SNTS stabilisé — opportunité à observer.",
        payload={
            "market_summary": "Clôture hebdo positive. BOAC et SGBC confirment. SNTS se stabilise sous 19 200 — à surveiller pour reprise technique.",
            "market_regime": "range",
            "opportunities": [
                {
                    "ticker": "SNTS", "name": "Sonatel", "sector": "Télécoms",
                    "direction": "watch", "conviction": 3, "time_horizon": "court",
                    "thesis": "Stabilisation technique après correction ARTCI. Reprise possible sur rebond bancaire ou clarification régulatoire.",
                    "signals": ["Volume décroissant vendeur"],
                    "price_current": PRICE_SERIES["SNTS"][4],
                    "price_target": 20000,
                    "gain_potential_pct": 4.71,
                },
            ],
            "alerts": [], "watchlist_updates": [], "skip_reasons": "",
        },
        revision=1,
        delivery_status="delivered",
    )
    session.add(b_fri)
    session.flush()
    session.add(Signal(
        brief_id=b_fri.id, ticker="SNTS", direction="watch",
        conviction=3, thesis="Stabilisation technique",
        price_at_signal=float(PRICE_SERIES["SNTS"][4]), signal_date=friday,
    ))
    briefs_out.append(b_fri)

    return briefs_out


def _seed_trades(session, trading_dates: list[datetime], briefs: list[Brief]) -> None:
    """2 trades utilisateur — 1 qui suit un signal (BOAC mardi),
    1 autonome (titre hors recos).

    Démontre la section trade_execution dans le weekly.
    """
    tuesday = trading_dates[1].replace(hour=10, minute=15, second=0, microsecond=0)
    thursday = trading_dates[3].replace(hour=14, minute=45, second=0, microsecond=0)

    b_mon = briefs[0]  # brief du lundi qui a émis le signal BOAC
    boac_signal = next((s for s in b_mon.signals if s.ticker == "BOAC"), None)

    session.add_all([
        Trade(
            ticker="BOAC", action="buy", quantity=50,
            unit_price=float(PRICE_SERIES["BOAC"][1]),  # 6550 (exécuté au cours de mardi)
            executed_at=tuesday, reason="brief",
            brief_id=b_mon.id, signal_id=boac_signal.id if boac_signal else None,
            notes="Suivi du call BOAC du lundi — ordre passé à l'ouverture mardi.",
        ),
        Trade(
            ticker="UNLC", action="buy", quantity=20,
            unit_price=8500.0,
            executed_at=thursday, reason="intuition",
            notes="Achat autonome — pas dans les recos de la semaine.",
        ),
    ])


# --- Main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--purge", action="store_true",
        help="Purger la fenêtre avant de seed (⚠ destructif)",
    )
    args = parser.parse_args()

    init_db()
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)

    week_end = _most_recent_friday(now)
    dates = _trading_dates(week_end)
    week_start = dates[0]

    print(f"Fenêtre ciblée : {week_start.date()} → {week_end.date()}")
    print(f"Tickers seedés : {[t['ticker'] for t in TICKERS]}")

    with get_session() as s:
        if args.purge:
            print("Purge de la fenêtre…")
            _purge_window(s, week_start, week_end + timedelta(days=1))
            s.flush()

        # Skip si déjà seedé (évite les doublons quotes/briefs)
        existing_briefs = s.execute(
            select(Brief)
            .where(Brief.brief_type == "daily")
            .where(Brief.brief_date >= week_start)
            .where(Brief.brief_date < week_end + timedelta(days=2))
        ).scalars().all()
        if existing_briefs and not args.purge:
            print(
                f"⚠ {len(list(existing_briefs))} brief(s) existent déjà sur la fenêtre. "
                "Utilise --purge pour remplacer."
            )
            return 1

        print("Seed quotes…")
        _seed_quotes(s, dates)

        print("Seed briefs + signals…")
        briefs = _seed_briefs(s, dates)
        s.flush()

        print("Seed trades utilisateur…")
        _seed_trades(s, dates, briefs)

    print(
        f"\n✓ Seed terminé. Déclenche maintenant le weekly :\n"
        f"  curl -X POST -H 'X-Admin-Token: {settings.admin_api_token}' "
        f"http://localhost:8000/api/schedule/run-weekly-now\n"
        f"Puis ouvre /briefs dans l'admin et filtre sur 'Hebdo'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
