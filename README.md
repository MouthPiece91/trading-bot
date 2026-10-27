# trading-bot

Felix's liquidity + candlestick strategy as a bot for MetaTrader 5:
higher-timeframe bias and order block / FVG zones, a liquidity sweep, price 50% into
the zone, then a candlestick confirmation from *The Candlestick Trading Bible* on
the lower timeframe (H4 -> M15 or H1 -> M5). Full rules: [STRATEGY.md](STRATEGY.md).

**Order of work: backtest -> dry run -> demo account -> real money.** The bot
enforces the risk rules in code: 3% hard cap per trade, daily loss stop, max
drawdown stop, trades-per-day limit, pause after a losing streak.

## Setup (Windows)

1. Install **MetaTrader 5** from your broker, log in to a **demo** account, and
   switch on **Algo Trading** (toolbar button).
2. In PowerShell, one line at a time:

```powershell
cd $HOME
git clone https://github.com/MouthPiece91/trading-bot
cd trading-bot
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[mt5,dev]"
pytest -q
```

## 1. Get your broker's contract details

```powershell
tradebot specs --symbol XAUUSD
```

Copy the printed block into `config.yaml` under `symbols:`. Use the exact symbol name
from Market Watch (some brokers add suffixes like `XAUUSD.m`). Do this for every
symbol you trade; the defaults in `config.yaml` are only typical values.

## 2. Download history

```powershell
tradebot fetch --symbol XAUUSD --tf M5 --days 365
tradebot fetch --symbol XAUUSD --tf M15 --days 730
```

If it returns few bars, scroll the chart back in MT5 or raise
Tools > Options > Charts > "Max bars in chart", then fetch again.

## 3. Backtest

```powershell
tradebot backtest --symbol XAUUSD --pair H1-M5
tradebot backtest --symbol XAUUSD --pair H1-M5 --compare
tradebot backtest --symbol XAUUSD --pair H4-M15 --compare
```

Reports and a trade-by-trade CSV go to `reports\`. `--compare` tests every entry
mode (`confirm_close`, `confirm_50`, `zone_limit`) against every management mode
(`none`, `breakeven`, `breakeven_partial`, `trail`).

Rules for trusting a result (from STRATEGY.md section 8): about 200+ trades,
positive average R **after costs**, profit factor above 1.3, and positive in both
halves of the data. Then confirm on a second symbol.

## 4. Dry run, then demo

Set `live_targets` in `config.yaml`, then:

```powershell
tradebot live            # DRY RUN: logs signals, sends nothing
tradebot live --live     # sends orders; refuses a real account unless allowed
```

Logs: `logs\tradebot.log`. To stop: create a file named `STOP` in the folder
(`New-Item STOP`). Open trades keep their stop loss and take profit.

Real money only after demo results match the backtest, by setting
`allow_real_account: true`.

## Honest notes

- The live/MT5 module is tested against a simulated MT5 here, not yet against your
  broker. Run it on a demo account first and watch the first trades closely.
- A good backtest is necessary but not sufficient: slippage, spread spikes at news
  and broker differences make live results worse. Expect that.
- Built and tested: candlestick detection, structure, zones, liquidity, sizing,
  management, backtester (including a no-look-ahead test and a random-data test
  that confirms the backtester does not invent an edge).
