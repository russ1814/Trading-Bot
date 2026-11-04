# Breakout Trading Bot (Alpaca)

An automated bot that watches a list of stocks/ETFs and trades breakouts on its own:

- **Buy** when a stock closes at a **new 52-week high**. If several break out the same day and there aren't enough slots, it buys the strongest ones first (biggest 3-month gain).
- **Sell quickly when it stalls:** when the price closes below its lowest close of the last **10 trading days**, or falls **7%** below what you paid (stop-loss).

It runs once each trading day, shortly before the market closes. It starts in **paper trading** (simulated money) and will not touch real money until you deliberately switch it over.

The older moving-average strategy is still included; set `strategy.name: ma_crossover` in `config.yaml` to use it.

> **Important:** This is a tool, not financial advice. Breakout trading is high-risk by design: expect to **lose on most trades** (often 55–70% of them) and rely on a few big winners to make up for it. Breakouts frequently fail and reverse, and in choppy or falling markets the strategy can lose steadily while an index fund holds up better. Backtest it, paper trade it for at least a few months, and only use money you can afford to lose.

---

## What's in the folder

| File | What it does |
|---|---|
| `config.yaml` | **The only file you normally edit.** Watch list, strategy settings, dollar limits, paper/live. |
| `bot.py` | The bot. Checks prices and places orders. |
| `strategy.py` | The buy/sell rules and the backtest engine. |
| `backtest.py` | Tests the rules on past prices. |
| `.github/workflows/run-bot.yml` | Runs the bot automatically every weekday for free on GitHub. |
| `tests/` | Automated tests (run with `pytest`). |

## Tuning it (`config.yaml`)

| Setting | Default | Effect of raising it |
|---|---|---|
| `symbols` | 20 large, heavily traded names | An **example** list, not picks. More names = more breakouts to choose from. |
| `lookback_days` | 252 (52 weeks) | Fewer, stronger breakouts. Try 126 (6 months) for more trades. |
| `exit_days` | 10 | Holds through bigger dips: catches longer runs, gives back more profit. |
| `stop_loss_pct` | 7 | More room before bailing; bigger losses when wrong. |
| `dollars_per_position` | 300 | Bigger bets per trade. |
| `max_positions` | 5 | More stocks held at once. |
| `max_total_invested` | 1500 | Hard ceiling on everything the bot holds. |

Change one thing at a time and re-run the backtest to see the effect.

## Safety features

- Paper trading by default. Live trading needs **two** switches: `paper: false` in the config **and** a secret `ALPACA_LIVE_CONFIRM=yes`.
- `dollars_per_position` caps each buy, `max_positions` caps how many stocks it holds, and `max_total_invested` caps the total.
- Sells are handled before buys. Money freed by a sale isn't reused until the next day's run, so an unfilled sell can never push it over its limits.
- Never buys a stock it already holds or has an order pending for, so extra runs can't double up.
- Stop-loss sells a position that's down more than `stop_loss_pct`.
- Kill switch: set `BOT_PAUSED=1` (or create a file named `STOP`) and it does nothing.
- Dry-run mode: `python bot.py --dry-run` shows what it would do without placing orders.
- Does nothing when the market is closed.
- Every decision is written to `trades.log`.

---

## Setup

### 1. Get an Alpaca paper account (free)
1. Sign up at **alpaca.markets**.
2. In the dashboard, make sure you're on the **Paper** account.
3. Generate an **API key** and **secret key**. Copy both somewhere safe; the secret is only shown once.

### 2. Try it on your computer
Requires Python 3.10+.

```bash
cd trading-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export ALPACA_API_KEY=your_key      # Windows: set ALPACA_API_KEY=your_key
export ALPACA_SECRET_KEY=your_secret
```

**Backtest first** (see how the rules would have done over the last 5 years):
```bash
python backtest.py                          # breakout, every symbol in your list
python backtest.py --years 8                # longer history, includes more market conditions
python backtest.py --strategy ma_crossover  # compare with the moving-average strategy
```
For each symbol it shows the strategy's return vs. simply holding, the worst drop, the number of trades, the win rate, and the average win and loss. At the end it sums up the whole list. Look hardest at the **worst drop** and **average loss**: those are what high-risk trading feels like.

**Then a dry run** (during market hours, 9:30am–4pm ET):
```bash
python bot.py --dry-run
```

**Then a real paper run:**
```bash
python bot.py
```
Check the Alpaca paper dashboard to see the orders.

### 3. Make it run automatically (free, no computer needed)
GitHub Actions runs the bot every weekday at about 3:43pm ET.

1. Create a **private** repository on github.com and upload this folder to it.
2. In the repo go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
3. Go to the **Actions** tab and enable workflows if asked.
4. To test right away: **Actions → Run trading bot → Run workflow**.
5. Each run's log is saved under that run as a downloadable `trades-log` file.

Notes:
- The schedule fires twice a day to handle daylight saving time. That's intentional and safe; one of the two runs will usually just report "market closed" or "hold".
- GitHub can delay scheduled runs by several minutes on busy days, and pauses schedules on repos with no activity for 60 days. Push a small change now and then to keep it active.

---

## Pausing the bot
- **On GitHub:** Settings → Secrets and variables → Actions → **Variables** tab → add `BOT_PAUSED` = `1`. Delete it to resume.
- **Locally:** create an empty file named `STOP` in the folder.
- To stop for good, disable the workflow in the Actions tab.

Pausing stops new trades; it doesn't sell what you already hold. Close positions in the Alpaca dashboard if you want out.

## Going live (real money)

Only after weeks or months of paper trading you're happy with:

1. Fund a live Alpaca account and create **live** API keys (different from paper keys).
2. Replace the GitHub secrets with the live keys.
3. Add a secret `ALPACA_LIVE_CONFIRM` = `yes`.
4. Set `paper: false` in `config.yaml` and push.
5. Consider lowering `dollars_per_position` and `max_total_invested` at first.

To go back to paper, set `paper: true`, restore the paper keys, and delete `ALPACA_LIVE_CONFIRM`.

## Things to know
- **Taxes:** every sell is a taxable event in a regular brokerage account. Frequent short-term trades are taxed at your ordinary income rate.
- **Pattern day trading:** the bot holds positions for days or weeks, so it shouldn't trigger the PDT rule, but keep it in mind if you change it.
- **Data:** uses Alpaca's free IEX price feed, which is fine for daily signals.
- **Backtests flatter you:** they ignore slippage, fees and taxes, and the past doesn't repeat exactly.

## Running the tests
```bash
pip install pytest
pytest -q
```
The tests use a fake Alpaca connection, so no account or keys are needed.
