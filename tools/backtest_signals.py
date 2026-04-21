"""Backtest des signaux brvm-agent (epic M-2).

Compare le PnL hypothétique si l'on avait suivi 100 % des signaux BUY à
horizons J+5 / J+15 / J+30, vs une baseline composite BRVM (moyenne non
pondérée des returns quotidiens sur tous les tickers avec cotation).

Usage :
    # Depuis brvm-agent-api/ avec venv actif :
    python tools/backtest_signals.py --output backtest.csv
    python tools/backtest_signals.py --since 2026-03-01 --min-conviction 3

Output :
  - CSV avec 1 ligne par signal : ticker, direction, conviction, price_at_signal,
    price_at_J+5/15/30, pnl_pct_*, baseline_pnl_pct_*, alpha_pct_*
  - Stats agrégées en stdout : moyenne, médiane, % de signaux gagnants

Limites explicites :
- On backtest uniquement les signaux `direction == "buy"`. Les "watch" et
  "hold" ne portent pas de PnL attendu.
- `price_at_signal` peut être NULL (brief généré avant la collecte du quote
  du jour) — ces signaux sont skippés et comptés séparément.
- La baseline utilise TOUS les tickers cotés sur la période, pas un indice
  pondéré par capitalisation. Approximation volontaire ; remplacer par
  BRVM Composite réel quand il sera collecté systématiquement.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, select

# Autorise l'exécution `python tools/backtest_signals.py` depuis la racine du package
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import get_session  # noqa: E402
from src.models import Quote, Signal  # noqa: E402

HORIZONS = [5, 15, 30]


@dataclass
class BacktestRow:
    signal_id: int
    ticker: str
    direction: str
    conviction: int
    signal_date: date
    price_at_signal: float | None
    # Prix à J+H et PnL % pour chaque horizon
    price_j5: float | None = None
    price_j15: float | None = None
    price_j30: float | None = None
    pnl_pct_j5: float | None = None
    pnl_pct_j15: float | None = None
    pnl_pct_j30: float | None = None
    baseline_pnl_pct_j5: float | None = None
    baseline_pnl_pct_j15: float | None = None
    baseline_pnl_pct_j30: float | None = None
    alpha_pct_j5: float | None = None
    alpha_pct_j15: float | None = None
    alpha_pct_j30: float | None = None
    note: str = ""


# --- Helpers ----------------------------------------------------------------


def _close_at_or_after(session, ticker: str, target: date) -> tuple[date, float] | None:
    """Retourne la 1re cotation (quote_date, close_price) >= target pour ce ticker.

    Permet de gérer les jours fériés / week-ends : si J+5 tombe un samedi, on
    prend le 1er jour ouvré suivant.
    """
    row = session.execute(
        select(Quote.quote_date, Quote.close_price)
        .where(
            and_(
                Quote.ticker == ticker,
                Quote.quote_date >= datetime.combine(target, datetime.min.time(), tzinfo=UTC),
                Quote.close_price > 0,
            )
        )
        .order_by(Quote.quote_date.asc())
        .limit(1)
    ).first()
    if row is None:
        return None
    qd = row[0].date() if hasattr(row[0], "date") else row[0]
    return qd, float(row[1])


def _baseline_return_pct(
    session, from_date: date, to_date: date, exclude_ticker: str | None = None
) -> float | None:
    """Return composite sur la fenêtre [from_date, to_date].

    Pour chaque ticker coté aux deux bornes, calcule `(close_to / close_from - 1) * 100`,
    puis moyenne non-pondérée. Exclut optionnellement le ticker du signal pour
    éviter qu'il ne se "compare à lui-même" dans la baseline (petit biais sur
    marché peu liquide).
    """
    tickers_from: dict[str, float] = dict(
        session.execute(
            select(Quote.ticker, Quote.close_price).where(
                and_(
                    Quote.quote_date >= datetime.combine(from_date, datetime.min.time(), tzinfo=UTC),
                    Quote.quote_date < datetime.combine(from_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
                    Quote.close_price > 0,
                )
            )
        ).all()
    )
    tickers_to: dict[str, float] = dict(
        session.execute(
            select(Quote.ticker, Quote.close_price).where(
                and_(
                    Quote.quote_date >= datetime.combine(to_date, datetime.min.time(), tzinfo=UTC),
                    Quote.quote_date < datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
                    Quote.close_price > 0,
                )
            )
        ).all()
    )
    returns: list[float] = []
    for t, p_from in tickers_from.items():
        if exclude_ticker and t == exclude_ticker:
            continue
        p_to = tickers_to.get(t)
        if p_to is None or p_from <= 0:
            continue
        returns.append((p_to / p_from - 1.0) * 100.0)
    if not returns:
        return None
    return statistics.mean(returns)


# --- Main -------------------------------------------------------------------


def run_backtest(
    since: date | None = None,
    min_conviction: int = 1,
    output_path: Path | None = None,
) -> list[BacktestRow]:
    """Exécute le backtest et retourne la liste des lignes."""
    rows: list[BacktestRow] = []

    with get_session() as s:
        stmt = select(Signal).where(Signal.direction == "buy")
        if since is not None:
            stmt = stmt.where(
                Signal.signal_date >= datetime.combine(since, datetime.min.time(), tzinfo=UTC)
            )
        if min_conviction > 1:
            stmt = stmt.where(Signal.conviction >= min_conviction)
        signals = list(s.execute(stmt.order_by(Signal.signal_date.asc())).scalars().all())

        for sig in signals:
            sig_date = sig.signal_date.date() if hasattr(sig.signal_date, "date") else sig.signal_date
            row = BacktestRow(
                signal_id=sig.id,
                ticker=sig.ticker,
                direction=sig.direction,
                conviction=sig.conviction,
                signal_date=sig_date,
                price_at_signal=sig.price_at_signal,
            )

            if sig.price_at_signal is None or sig.price_at_signal <= 0:
                row.note = "price_at_signal manquant — skipped"
                rows.append(row)
                continue

            for h in HORIZONS:
                target = sig_date + timedelta(days=h)
                hit = _close_at_or_after(s, sig.ticker, target)
                if hit is None:
                    setattr(row, f"price_j{h}", None)
                    continue
                actual_date, px = hit
                setattr(row, f"price_j{h}", px)
                pnl = (px / sig.price_at_signal - 1.0) * 100.0
                setattr(row, f"pnl_pct_j{h}", round(pnl, 3))

                baseline = _baseline_return_pct(
                    s, sig_date, actual_date, exclude_ticker=sig.ticker
                )
                if baseline is not None:
                    setattr(row, f"baseline_pnl_pct_j{h}", round(baseline, 3))
                    setattr(row, f"alpha_pct_j{h}", round(pnl - baseline, 3))

            rows.append(row)

    # --- Export CSV ---
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(BacktestRow.__dataclass_fields__.keys()))
            writer.writeheader()
            for r in rows:
                writer.writerow(asdict(r))

    return rows


def _print_stats(rows: list[BacktestRow]) -> None:
    total = len(rows)
    with_price = [r for r in rows if r.price_at_signal is not None]
    skipped = total - len(with_price)

    print()
    print(f"=== Backtest signaux BRVM — {total} signaux BUY ===")
    if skipped:
        print(f"  [!] {skipped} signaux skippés (price_at_signal manquant)")

    for h in HORIZONS:
        pnl_key = f"pnl_pct_j{h}"
        alpha_key = f"alpha_pct_j{h}"
        pnls = [getattr(r, pnl_key) for r in with_price if getattr(r, pnl_key) is not None]
        alphas = [getattr(r, alpha_key) for r in with_price if getattr(r, alpha_key) is not None]

        if not pnls:
            print(f"\n  J+{h} : aucune donnée (pas assez d'historique)")
            continue

        wins = sum(1 for p in pnls if p > 0)
        print(f"\n  J+{h} ({len(pnls)} signaux exploitables) :")
        print(f"    PnL moyen      : {statistics.mean(pnls):+.2f} %")
        print(f"    PnL médian     : {statistics.median(pnls):+.2f} %")
        print(f"    Taux gagnants  : {wins}/{len(pnls)} ({100*wins/len(pnls):.0f} %)")
        if alphas:
            alpha_mean = statistics.mean(alphas)
            positive_alpha = sum(1 for a in alphas if a > 0)
            print(f"    Alpha moyen    : {alpha_mean:+.2f} % vs baseline BRVM composite")
            print(f"    Alpha positif  : {positive_alpha}/{len(alphas)} ({100*positive_alpha/len(alphas):.0f} %)")

    print()
    print("Interprétation rapide :")
    print("  - Alpha moyen > +2% sur J+15 et J+30 → signal a de la valeur, continuer")
    print("  - Alpha moyen entre -2% et +2%    → tie, revoir prompt ou abandonner")
    print("  - Alpha moyen < -2%               → signaux nuisibles, tuer le projet")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest signaux brvm-agent")
    parser.add_argument(
        "--since",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).date(),
        default=None,
        help="Date de début (YYYY-MM-DD). Par défaut : tous les signaux.",
    )
    parser.add_argument(
        "--min-conviction",
        type=int,
        default=1,
        help="Ignore les signaux de conviction < N (défaut 1 = tous).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("backtest.csv"),
        help="Fichier CSV de sortie (défaut : ./backtest.csv).",
    )
    args = parser.parse_args()

    rows = run_backtest(
        since=args.since,
        min_conviction=args.min_conviction,
        output_path=args.output,
    )
    _print_stats(rows)
    print(f"\nCSV détaillé : {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
