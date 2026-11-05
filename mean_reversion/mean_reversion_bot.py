"""
Mean Reversion Bot (RSI-2 "buy the dip in an uptrend")
-------------------------------------------------------
Runs once a day ~15-30 min before the close via GitHub Actions.

Rules
  Market filter : only open new trades when SPY is above its 200-day average
  Stock filter  : stock must be above its own 200-day average (long-term uptrend)
  Entry         : 2-day RSI below RSI_ENTRY (a sharp short-term selloff)
  Exit (any)    : price closes above its 5-day average   -> bounce happened
                  2-day RSI above RSI_EXIT               -> overbought
                  held MAX_HOLD_DAYS trading days        -> time stop
                  down STOP_LOSS from entry              -> stop loss
  Sizing        : POSITION_PCT of equity per trade, max MAX_POSITIONS at once
"""

import datetime as dt
import math
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

# ---------------- settings (tweak these) ----------------
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V",
    "MA", "UNH", "XOM", "JNJ", "PG", "HD", "COST", "ABBV", "MRK", "PEP",
    "KO", "WMT", "BAC", "CVX", "LLY", "ORCL", "CRM", "ADBE", "AMD", "NFLX",
    "CSCO", "TMO", "ABT", "MCD", "DIS", "QCOM", "TXN", "HON", "CAT", "GS",
    "MS", "IBM", "AMGN", "LOW", "UNP", "SPGI", "BLK", "NKE", "SBUX", "ISRG",
]
MARKET_SYMBOL = "SPY"

MAX_POSITIONS = 10
POSITION_PCT = 0.10       # 10% of equity per position
RSI_ENTRY = 10
RSI_EXIT = 70
MAX_HOLD_DAYS = 7
STOP_LOSS = 0.08          # 8%
TRADE_WINDOW_MIN = (2, 45)  # only trade when the close is 2-45 minutes away
# --------------------------------------------------------

ET = ZoneInfo("America/New_York")
API_KEY = os.environ["ALPACA_MR_API_KEY"]
SECRET_KEY = os.environ["ALPACA_MR_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
FORCE = os.environ.get("FORCE_RUN", "false").lower() == "true"

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def log(msg):
    print(f"[{dt.datetime.now(ET):%Y-%m-%d %H:%M ET}] {msg}", flush=True)


def rsi(closes: pd.Series, n: int = 2) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def load_closes(symbols):
    
    start = dt.datetime.now(ET) - dt.timedelta(days=420)
    bars = data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start,
        adjustment=Adjustment.ALL, feed=DataFeed.IEX,
    )).df
    latest = data.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX))

    today = dt.datetime.now(ET).date()
    out = {}
    for sym in symbols:
        if sym not in bars.index.get_level_values(0):
            continue
        s = bars.loc[sym]["close"].copy()
        s.index = pd.to_datetime(s.index).tz_convert(ET).date
        if sym in latest:
            s.loc[today] = float(latest[sym].price)  # adds or overwrites today
        out[sym] = s.sort_index()
    return out


def signals(closes: pd.Series):
    return {
        "price": closes.iloc[-1],
        "sma200": closes.rolling(200).mean().iloc[-1],
        "sma5": closes.rolling(5).mean().iloc[-1],
        "rsi2": rsi(closes).iloc[-1],
    }


def trading_days_held(symbol):
    orders = trading.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide.BUY, limit=20))
    fills = [o.filled_at for o in orders if o.filled_at]
    if not fills:
        return 0
    entry = max(fills).astimezone(ET).date()
    return int(np.busday_count(entry, dt.datetime.now(ET).date()))


def submit(symbol, qty, side):
    log(f"{'DRY RUN ' if DRY_RUN else ''}{side.value.upper()} {qty} {symbol}")
    if not DRY_RUN:
        trading.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY))


def main():
    clock = trading.get_clock()
    if not clock.is_open:
        log("Market closed - nothing to do.")
        return
    mins_left = (clock.next_close - clock.timestamp).total_seconds() / 60
    lo, hi = TRADE_WINDOW_MIN
    if not (lo <= mins_left <= hi) and not FORCE:
        log(f"{mins_left:.0f} min to close - outside trade window, exiting.")
        return

    account = trading.get_account()
    equity = float(account.equity)
    log(f"Equity ${equity:,.2f} | paper={PAPER} | dry_run={DRY_RUN}")

    positions = {p.symbol: p for p in trading.get_all_positions()}
    pending = {o.symbol for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))}
    closes = load_closes(sorted(set(UNIVERSE) | set(positions) | {MARKET_SYMBOL}))

    # ---- exits ----
    freed_cash = 0.0
    for sym, pos in positions.items():
        if sym in pending or sym not in closes:
            continue
        sig = signals(closes[sym])
        entry_price = float(pos.avg_entry_price)
        drawdown = 1 - sig["price"] / entry_price
        held = trading_days_held(sym)
        reason = None
        if sig["price"] > sig["sma5"]:
            reason = "closed above 5-day avg"
        elif sig["rsi2"] > RSI_EXIT:
            reason = f"RSI2 {sig['rsi2']:.0f} > {RSI_EXIT}"
        elif held >= MAX_HOLD_DAYS:
            reason = f"time stop ({held} days)"
        elif drawdown >= STOP_LOSS:
            reason = f"stop loss ({drawdown:.1%})"
        if reason:
            log(f"EXIT {sym}: {reason}")
            submit(sym, abs(int(float(pos.qty))), OrderSide.SELL)
            freed_cash += float(pos.market_value)
            positions[sym] = None
    positions = {k: v for k, v in positions.items() if v is not None}

    # ---- entries ----
    mkt = signals(closes[MARKET_SYMBOL])
    if mkt["price"] < mkt["sma200"]:
        log(f"SPY below 200-day avg ({mkt['price']:.2f} < {mkt['sma200']:.2f}) - no new buys.")
        return

    slots = MAX_POSITIONS - len(positions)
    if slots <= 0:
        log("Portfolio full.")
        return

    candidates = []
    for sym in UNIVERSE:
        if sym in positions or sym in pending or sym not in closes or len(closes[sym]) < 200:
            continue
        sig = signals(closes[sym])
        if sig["price"] > sig["sma200"] and sig["rsi2"] < RSI_ENTRY:
            candidates.append((sig["rsi2"], sym, sig["price"]))
    candidates.sort()  # most oversold first
    log(f"{len(candidates)} candidates: {[c[1] for c in candidates]}")

    cash = float(account.cash) + freed_cash
    for _, sym, price in candidates[:slots]:
        budget = min(equity * POSITION_PCT, cash)
        qty = math.floor(budget / price)
        if qty < 1:
            log("Out of cash.")
            break
        submit(sym, qty, OrderSide.BUY)
        cash -= qty * price


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
