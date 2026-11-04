"""Test the strategy on past prices before letting it trade.

Usage:
    python backtest.py                        # all symbols in config.yaml, last 5 years (needs Alpaca keys)
    python backtest.py --years 8
    python backtest.py --strategy ma_crossover
    python backtest.py --csv prices.csv       # offline: CSV with "date" and "close" columns

Results ignore fees, slippage and taxes. Past performance does not predict future results.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import yaml

from strategy import backtest


def fetch(symbols: list[str], years: int) -> dict:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    start = datetime.now(timezone.utc) - timedelta(days=365 * years)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start,
                           feed=DataFeed.IEX, adjustment=Adjustment.ALL)  # adjust for splits/dividends
    df = client.get_stock_bars(req).df
    have = set(df.index.get_level_values(0))
    return {s: df.loc[s]["close"] for s in symbols if s in have}


def fmt(v, suffix="%"):
    return "   n/a" if v is None else f"{v:>6.1f}{suffix}"


def report(name: str, r: dict) -> None:
    print(f"{name:<8} strategy {fmt(r['strategy_return_pct'])} | buy&hold {fmt(r['buy_and_hold_return_pct'])}"
          f" | worst drop {fmt(r['max_drawdown_pct'])} | trades {r['round_trips']:>3}"
          f" | win rate {fmt(r['win_rate_pct'])} | avg win {fmt(r['avg_win_pct'])}"
          f" | avg loss {fmt(r['avg_loss_pct'])}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--csv")
    p.add_argument("--strategy", choices=["breakout", "ma_crossover"])
    args = p.parse_args()

    with open(os.path.join(os.path.dirname(__file__), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    strat = dict(cfg["strategy"])
    if args.strategy:
        strat["name"] = args.strategy
    stop = float(cfg["risk"].get("stop_loss_pct", 0))
    if strat["name"] == "breakout":
        rules = f"buy new {strat.get('lookback_days', 252)}-day high, sell below {strat.get('exit_days', 10)}-day low"
    else:
        rules = f"{strat.get('short_window', 20)}-day vs {strat.get('long_window', 50)}-day average"
    print(f"Strategy: {strat['name']} ({rules}), stop-loss {stop or 'off'}%\n")

    data = ({args.csv: pd.read_csv(args.csv, parse_dates=["date"]).set_index("date")["close"]}
            if args.csv else fetch(cfg["symbols"], args.years))

    results = {}
    for sym, closes in data.items():
        results[sym] = r = backtest(closes, strat, stop_loss_pct=stop)
        report(sym, r)

    if len(results) > 1:
        # Equal money in every symbol, each run by the strategy vs. simply held.
        strat_eq = pd.concat([r["equity"] / r["equity"].iloc[0] for r in results.values()], axis=1).ffill().mean(axis=1)
        hold_eq = pd.concat([c / c.iloc[0] for c in data.values()], axis=1).ffill().mean(axis=1)
        dd = ((strat_eq - strat_eq.cummax()) / strat_eq.cummax()).min() * 100
        hdd = ((hold_eq - hold_eq.cummax()) / hold_eq.cummax()).min() * 100
        trips = [t for r in results.values() for t in [r["round_trips"]]]
        wins = [r["win_rate_pct"] for r in results.values() if r["win_rate_pct"] is not None]
        print(f"\nWHOLE LIST (equal money in each):")
        print(f"  Strategy:   {(strat_eq.iloc[-1] - 1) * 100:>7.1f}%   worst drop {dd:.1f}%")
        print(f"  Buy & hold: {(hold_eq.iloc[-1] - 1) * 100:>7.1f}%   worst drop {hdd:.1f}%")
        print(f"  Total round-trip trades: {sum(trips)}, average win rate {sum(wins) / max(1, len(wins)):.0f}%")
        print("\nNote: this backtest puts money in every symbol at once. The live bot is capped by"
              "\nmax_positions and max_total_invested, so it will hold fewer names at a time.")


if __name__ == "__main__":
    main()
