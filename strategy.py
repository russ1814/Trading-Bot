"""Strategy logic and backtester. No broker code here, so it can be tested offline.

Two strategies, picked with `strategy.name` in config.yaml:

  breakout      Buy when the price closes at a new N-day high (default 252 days = 52 weeks).
                Sell when it closes below its lowest close of the last M days (default 10),
                i.e. the move has stalled, or when it hits the stop-loss.

  ma_crossover  Buy when the short moving average is above the long one, sell when below.

All rules look only at prices and what you currently hold, so the bot needs no
memory between runs: a missed day never loses a trade, and an extra run never
doubles one.
"""
from __future__ import annotations

import pandas as pd

DEFAULTS = {
    "breakout": {"lookback_days": 252, "exit_days": 10},
    "ma_crossover": {"short_window": 20, "long_window": 50},
}


def history_needed(strat: dict) -> int:
    """How many daily bars the strategy needs."""
    if strat["name"] == "breakout":
        return int(strat.get("lookback_days", 252)) + 1
    return int(strat.get("long_window", 50))


# ---------------- moving-average crossover ----------------
def signal(closes: pd.Series, short_window: int, long_window: int) -> str:
    """Return "bullish", "bearish", or "insufficient_data"."""
    if short_window >= long_window:
        raise ValueError("short_window must be smaller than long_window")
    closes = closes.dropna()
    if len(closes) < long_window:
        return "insufficient_data"
    short_ma = closes.iloc[-short_window:].mean()
    long_ma = closes.iloc[-long_window:].mean()
    return "bullish" if short_ma > long_ma else "bearish"


def decide(sig: str, holding: bool, unrealized_pct: float | None, stop_loss_pct: float) -> str:
    """Moving-average signal + current position -> "buy", "sell" or "hold"."""
    if holding and stop_loss_pct > 0 and unrealized_pct is not None and unrealized_pct <= -stop_loss_pct:
        return "sell"
    if sig == "bullish" and not holding:
        return "buy"
    if sig == "bearish" and holding:
        return "sell"
    return "hold"


# ---------------- breakout ----------------
def breakout_action(closes: pd.Series, holding: bool, avg_cost: float | None,
                    lookback_days: int, exit_days: int, stop_loss_pct: float) -> str:
    """Breakout rules. The last value in `closes` is today's (or the latest) price."""
    if exit_days >= lookback_days:
        raise ValueError("exit_days must be smaller than lookback_days")
    closes = closes.dropna()
    price = closes.iloc[-1] if len(closes) else None

    if holding:
        if price is None:
            return "hold"
        if stop_loss_pct > 0 and avg_cost and price <= avg_cost * (1 - stop_loss_pct / 100):
            return "sell"                                   # stop-loss
        if len(closes) > exit_days and price < closes.iloc[-exit_days - 1:-1].min():
            return "sell"                                   # stalled: broke its recent low
        return "hold"

    if len(closes) < lookback_days + 1:
        return "hold"                                       # not enough history
    if price > closes.iloc[-lookback_days - 1:-1].max():
        return "buy"                                        # new 52-week high
    return "hold"


def breakout_strength(closes: pd.Series, days: int = 63) -> float:
    """% gain over ~3 months. Used to pick the strongest breakouts when slots are limited."""
    closes = closes.dropna()
    if len(closes) <= days:
        return 0.0
    return (closes.iloc[-1] / closes.iloc[-days - 1] - 1) * 100


# ---------------- dispatcher ----------------
def action_for(strat: dict, closes: pd.Series, holding: bool,
               avg_cost: float | None, stop_loss_pct: float) -> str:
    name = strat["name"]
    if name == "breakout":
        return breakout_action(closes, holding, avg_cost,
                               int(strat.get("lookback_days", 252)),
                               int(strat.get("exit_days", 10)), stop_loss_pct)
    if name == "ma_crossover":
        price = closes.dropna().iloc[-1] if len(closes.dropna()) else None
        pnl = (price / avg_cost - 1) * 100 if holding and avg_cost and price else None
        sig = signal(closes, int(strat.get("short_window", 20)), int(strat.get("long_window", 50)))
        return decide(sig, holding, pnl, stop_loss_pct)
    raise ValueError(f"Unknown strategy: {name!r} (use 'breakout' or 'ma_crossover')")


# ---------------- backtest ----------------
def backtest(closes: pd.Series, strat: dict, starting_cash: float = 10_000.0,
             stop_loss_pct: float = 0.0, idle_closes: pd.Series | None = None) -> dict:
    """Simulate one symbol, all-in / all-out, trading at each day's close.

    idle_closes: if given (e.g. SPY prices), money not in a trade is held in
    that fund instead of cash, like the bot's sweep_idle_cash setting.

    Ignores commissions, slippage and taxes, so real results will be worse.
    """
    closes = closes.dropna().astype(float)
    idle = (idle_closes.astype(float).reindex(closes.index).ffill().bfill()
            if idle_closes is not None else pd.Series(1.0, index=closes.index))
    idle_units, shares, cost = starting_cash / idle.iloc[0], 0.0, 0.0
    equity, trades, results = [], [], []

    for i in range(len(closes)):
        price, idle_price = closes.iloc[i], idle.iloc[i]
        holding = shares > 0
        act = action_for(strat, closes.iloc[: i + 1], holding, cost if holding else None, stop_loss_pct)
        if act == "buy":
            shares, cost, idle_units = idle_units * idle_price / price, price, 0.0
            trades.append((closes.index[i], "BUY", price))
        elif act == "sell":
            idle_units, shares = shares * price / idle_price, 0.0
            trades.append((closes.index[i], "SELL", price))
            results.append(price / cost - 1)
        equity.append(idle_units * idle_price + shares * price)

    equity = pd.Series(equity, index=closes.index)
    peak = equity.cummax()
    wins = [r for r in results if r > 0]
    return {
        "start": str(closes.index[0])[:10],
        "end": str(closes.index[-1])[:10],
        "strategy_return_pct": round((equity.iloc[-1] / starting_cash - 1) * 100, 2),
        "buy_and_hold_return_pct": round((closes.iloc[-1] / closes.iloc[0] - 1) * 100, 2),
        "max_drawdown_pct": round(((equity - peak) / peak).min() * 100, 2),
        "num_trades": len(trades),
        "round_trips": len(results),
        "win_rate_pct": round(len(wins) / len(results) * 100, 1) if results else None,
        "avg_win_pct": round(sum(wins) / len(wins) * 100, 2) if wins else None,
        "avg_loss_pct": round(sum(r for r in results if r <= 0) / max(1, len(results) - len(wins)) * 100, 2)
                        if len(results) > len(wins) else None,
        "still_holding": shares > 0,
        "trades": trades,
        "equity": equity,
    }
