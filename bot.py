"""Automated trading bot for Alpaca (breakout or moving-average strategy).

Run once per trading day, shortly before the close. Each run:
  1. Checks the market is open and the account can trade.
  2. Sells held positions whose exit rule fired (stalled breakout or stop-loss).
  3. Buys new breakouts, strongest first, within the limits in config.yaml.

Usage:
    python bot.py               # normal run
    python bot.py --dry-run     # show what it would do, place no orders
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from strategy import action_for, breakout_strength, history_needed

HERE = Path(__file__).parent
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "trades.log")],
)
log = logging.getLogger("bot")


def load_config() -> dict:
    with open(HERE / "config.yaml") as f:
        return yaml.safe_load(f)


def make_clients(cfg: dict) -> tuple[TradingClient, StockHistoricalDataClient]:
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (see README).")

    paper = bool(cfg.get("paper", True))
    if not paper and os.environ.get("ALPACA_LIVE_CONFIRM") != "yes":
        sys.exit("config says paper: false, but ALPACA_LIVE_CONFIRM=yes is not set. Refusing to trade real money.")

    log.info("Mode: %s", "PAPER (simulated money)" if paper else "LIVE (REAL MONEY)")
    return TradingClient(key, secret, paper=paper), StockHistoricalDataClient(key, secret)


def daily_closes(data: StockHistoricalDataClient, symbols: list[str], bars_needed: int) -> dict:
    """Daily closing prices for every symbol in one request. Missing symbols are left out."""
    start = datetime.now(timezone.utc) - timedelta(days=int(bars_needed * 1.5) + 15)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start,
                           feed=DataFeed.IEX, adjustment=Adjustment.ALL)  # adjust for splits/dividends
    df = data.get_stock_bars(req).df
    if df.empty:
        return {}
    have = set(df.index.get_level_values(0))
    return {s: df.loc[s]["close"] for s in symbols if s in have}


def buy_order(trading: TradingClient, symbol: str, dollars: float, price: float):
    """A market buy for about `dollars` of `symbol`. Returns (order, amount) or None."""
    asset = trading.get_asset(symbol)
    if not asset.tradable:
        log.warning("%s: not tradable on Alpaca, skipping.", symbol)
        return None
    if asset.fractionable:
        return MarketOrderRequest(symbol=symbol, notional=round(dollars, 2), side=OrderSide.BUY,
                                  time_in_force=TimeInForce.DAY), round(dollars, 2)
    qty = math.floor(dollars / price)
    if qty < 1:
        log.info("%s: $%.0f buys less than one share at $%.2f, skipping.", symbol, dollars, price)
        return None
    return MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY,
                              time_in_force=TimeInForce.DAY), qty * price


def plan_core(core: dict, spy_value: float, trading_after: float, max_total: float,
              breakout_buys: float) -> tuple[str, float, float]:
    """Decide what to do with the core fund. Returns (action, dollars, target).

    target = core dollars, plus (if sweep_idle_cash) whatever part of the trading
    budget isn't in breakout positions. The bot buys the core up to target when it's
    more than top_up_below_pct short, and sells it ONLY to fund new breakout buys,
    never below target. Gains above target are left alone.
    """
    target = float(core["dollars"])
    if core.get("sweep_idle_cash"):
        target += max(0.0, max_total - trading_after)
    band = float(core.get("top_up_below_pct", 5)) / 100
    delta = target - spy_value
    if delta > target * band and delta >= 1:
        return "buy", round(delta, 2), target
    if core.get("sweep_idle_cash") and breakout_buys > 0 and delta < 0:
        amount = round(min(breakout_buys, -delta), 2)
        if amount >= 1:
            return "sell", amount, target
    return "hold", 0.0, target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    dry_run = args.dry_run or bool(cfg.get("dry_run", False))
    strat = cfg["strategy"]

    # Kill switch: create a file named STOP next to this script (or set BOT_PAUSED=1).
    if (HERE / "STOP").exists() or os.environ.get("BOT_PAUSED") == "1":
        log.warning("Kill switch is on (STOP file or BOT_PAUSED=1). Doing nothing.")
        return

    trading, data = make_clients(cfg)

    if not trading.get_clock().is_open:
        log.info("Market is closed. Nothing to do.")
        return

    account = trading.get_account()
    if account.trading_blocked or account.account_blocked:
        log.error("Account is blocked from trading. Stopping.")
        return

    risk = cfg["risk"]
    per_position = float(risk["dollars_per_position"])
    max_total = float(risk["max_total_invested"])
    max_positions = int(risk.get("max_positions", 999))
    stop_loss = float(risk.get("stop_loss_pct", 0))
    buying_power = float(account.buying_power)

    all_positions = {p.symbol: p for p in trading.get_all_positions()}
    all_orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))

    core = cfg.get("core") or {}
    core_symbol = core.get("symbol") if float(core.get("dollars", 0) or 0) > 0 else None

    # The trading slice: everything except the core fund.
    positions = {s: p for s, p in all_positions.items() if s != core_symbol}
    open_orders = [o for o in all_orders if o.symbol != core_symbol]
    pending = {o.symbol for o in open_orders}
    pending_buys = [o for o in open_orders if o.side == OrderSide.BUY]

    invested = sum(abs(float(p.market_value)) for p in positions.values())
    invested += sum(float(o.notional or 0) for o in pending_buys)
    invested_start = invested
    slots_used = len(positions) + len({o.symbol for o in pending_buys} - set(positions))
    log.info("Trading (%s): invested $%.2f of $%.2f | %d of %d positions",
             strat["name"], invested, max_total, slots_used, max_positions)

    symbols = list(dict.fromkeys(cfg["symbols"]))
    if core_symbol in symbols:
        log.warning("%s is the core holding, so it's left out of trading.", core_symbol)
        symbols.remove(core_symbol)
    prices = daily_closes(data, symbols, history_needed(strat))
    for s in symbols:
        if s not in prices:
            log.warning("%s: no price data, skipping.", s)

    # ---- Pass 1: sells (checked first so exits never wait on buys) ----
    buy_candidates, sold_value = [], 0.0
    for symbol, closes in prices.items():
        try:
            if symbol in pending:
                log.info("%s: order already pending, skipping.", symbol)
                continue
            pos = positions.get(symbol)
            avg_cost = float(pos.avg_entry_price) if pos else None
            action = action_for(strat, closes, pos is not None, avg_cost, stop_loss)
            price = float(closes.iloc[-1])

            if action == "sell":
                pnl = (price / avg_cost - 1) * 100 if avg_cost else 0.0
                why = "stop-loss" if stop_loss and pnl <= -stop_loss else "exit signal"
                if dry_run:
                    log.info("%s: [DRY RUN] would SELL at ~$%.2f (%+.1f%%, %s)", symbol, price, pnl, why)
                else:
                    trading.close_position(symbol)
                    log.info("%s: SELL sent at ~$%.2f (%+.1f%%, %s)", symbol, price, pnl, why)
                sold_value += abs(float(pos.market_value))
            elif action == "buy":
                buy_candidates.append((breakout_strength(closes), symbol, price))
            else:
                log.info("%s: $%.2f, %s -> HOLD", symbol, price, "holding" if pos else "no signal")
        except Exception as exc:  # one bad symbol shouldn't stop the others
            log.exception("%s: error, skipped: %s", symbol, exc)

    # ---- Pass 2: choose buys, strongest first ----
    # Capacity freed by today's sells is NOT reused until the next run, so a
    # sell that hasn't filled yet can never let the bot exceed its limits.
    buy_candidates.sort(reverse=True)
    if buy_candidates:
        log.info("Buy signals (strongest first): %s",
                 ", ".join(f"{s} ({st:+.0f}% 3mo)" for st, s, _ in buy_candidates))
    planned = []
    for strength, symbol, price in buy_candidates:
        try:
            if slots_used >= max_positions:
                log.info("%s: buy skipped, already at max_positions (%d).", symbol, max_positions)
                continue
            if invested + per_position > max_total:
                log.info("%s: buy skipped, would exceed max_total_invested.", symbol)
                continue
            made = buy_order(trading, symbol, per_position, price)
            if made is None:
                continue
            planned.append((symbol, price, *made))
            invested += made[1]
            slots_used += 1
        except Exception as exc:
            log.exception("%s: error, skipped: %s", symbol, exc)
    breakout_total = sum(p[3] for p in planned)

    # ---- Pass 3: plan the core fund ----
    core_action, core_amount = "hold", 0.0
    if core_symbol:
        if any(o.symbol == core_symbol for o in all_orders):
            log.info("CORE %s: order already pending, skipping.", core_symbol)
        else:
            held = float(all_positions[core_symbol].market_value) if core_symbol in all_positions else 0.0
            trading_after = invested_start - sold_value + breakout_total
            core_action, core_amount, target = plan_core(core, held, trading_after, max_total, breakout_total)
            log.info("CORE %s: holding $%.2f, target $%.2f%s -> %s", core_symbol, held, target,
                     " (incl. idle trading cash)" if core.get("sweep_idle_cash") else "", core_action.upper())
            if core_action != "hold":
                asset = trading.get_asset(core_symbol)
                if not (asset.tradable and asset.fractionable):
                    log.warning("CORE %s: must be a tradable, fractional-share fund. Skipping.", core_symbol)
                    core_action = "hold"

    def send(order, text):
        if dry_run:
            log.info("[DRY RUN] would %s", text)
        else:
            trading.submit_order(order)
            log.info("Sent: %s", text)

    # ---- Execute: core sell first (funds breakouts), then breakout buys, then core buy ----
    if core_action == "sell":
        send(MarketOrderRequest(symbol=core_symbol, notional=core_amount, side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY),
             f"SELL ${core_amount:.2f} of {core_symbol} to fund breakouts")
        buying_power += core_amount

    for symbol, price, order, amount in planned:
        if buying_power < amount:
            log.info("%s: buy skipped, not enough buying power.", symbol)
            continue
        send(order, f"BUY ~${amount:.2f} of {symbol} at ~${price:.2f} (new high)")
        buying_power -= amount

    if core_action == "buy":
        amount = round(min(core_amount, buying_power), 2)
        if amount >= 1:
            send(MarketOrderRequest(symbol=core_symbol, notional=amount, side=OrderSide.BUY,
                                    time_in_force=TimeInForce.DAY),
                 f"BUY ${amount:.2f} of {core_symbol} (core)")
        else:
            log.info("CORE %s: no buying power left, skipping.", core_symbol)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
