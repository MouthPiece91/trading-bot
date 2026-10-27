"""Command line.

  tradebot fetch --symbol XAUUSD --tf M5 --days 365     download history from MT5 to data/
  tradebot specs --symbol XAUUSD                         print the broker's contract details
  tradebot backtest --symbol XAUUSD --pair H1-M5         test the strategy on that history
  tradebot backtest --symbol XAUUSD --pair H1-M5 --compare
                                                         every entry mode x management mode
  tradebot live                                          DRY RUN on the live_targets in config
  tradebot live --live                                   send real orders (DEMO account first)
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

from . import backtest as bt
from .bars import PAIRS, TIMEFRAMES, load_csv, resample, write_csv
from .config import ENTRY_MODES, MANAGEMENT_MODES, load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradebot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--symbol", required=True)
    f.add_argument("--tf", default="M5", choices=TIMEFRAMES)
    f.add_argument("--days", type=int, default=365)

    sp = sub.add_parser("specs")
    sp.add_argument("--symbol", required=True)

    b = sub.add_parser("backtest")
    b.add_argument("--symbol", required=True)
    b.add_argument("--pair", default="H1-M5", choices=PAIRS)
    b.add_argument("--data", help="CSV of lower-timeframe bars (default data/<SYMBOL>_<LTF>.csv)")
    b.add_argument("--entry-mode", choices=ENTRY_MODES)
    b.add_argument("--management", choices=MANAGEMENT_MODES)
    b.add_argument("--compare", action="store_true", help="run every entry x management combination")

    lv = sub.add_parser("live")
    lv.add_argument("--live", action="store_true", help="send real orders (default is dry run)")
    lv.add_argument("--poll", type=int, default=5)

    a = ap.parse_args(argv)
    cfg = load(a.config)

    try:
        if a.cmd == "fetch":
            from . import mt5
            mt5.connect()
            bars = mt5.history(a.symbol, a.tf, a.days)
            spec = mt5.spec(a.symbol)
            Path("data").mkdir(exist_ok=True)
            out = Path("data") / f"{a.symbol}_{a.tf}.csv"
            write_csv(bars, out, spec.point)
            print(f"Saved {len(bars):,} {a.tf} bars ({bars[0].time} to {bars[-1].time}) to {out}")
            mt5.shutdown()
        elif a.cmd == "specs":
            from . import mt5
            mt5.connect()
            s = mt5.spec(a.symbol)
            print(f"Account: {mt5.account_summary()}")
            print("Add this under 'symbols:' in config.yaml:\n")
            print(f"  {a.symbol}:\n    contract_size: {s.contract_size}\n    point: {s.point}\n"
                  f"    volume_min: {s.volume_min}\n    volume_step: {s.volume_step}\n    volume_max: {s.volume_max}\n"
                  f"    conversion: direct        # use divide_by_price for USDJPY, USDCAD, USDCHF\n"
                  f"    commission_per_lot_side: 0.0   # check your broker\n"
                  f"    default_spread_points: {s.default_spread_points}")
            print(f"\nBroker value of a 1.0 price move per lot: {mt5.value_per_unit(a.symbol):.4f}")
            mt5.shutdown()
        elif a.cmd == "backtest":
            htf, ltf = PAIRS[a.pair]
            path = Path(a.data or f"data/{a.symbol}_{ltf}.csv")
            spec = cfg.spec(a.symbol)
            cfg = cfg.for_symbol(a.symbol)
            bars = load_csv(path, spec.point)
            if bars and (bars[1].time - bars[0].time).seconds // 60 < TIMEFRAMES[ltf]:
                bars = resample(bars, TIMEFRAMES[ltf])
            Path("reports").mkdir(exist_ok=True)
            if a.compare:
                _compare(bars, a.symbol, htf, ltf, cfg)
            else:
                if a.entry_mode:
                    cfg.strategy.entry_mode = a.entry_mode
                if a.management:
                    cfg.management.mode = a.management
                res = bt.run(bars, a.symbol, htf, ltf, cfg)
                text = bt.report(res, cfg)
                stem = f"reports/{a.symbol}_{a.pair}_{cfg.strategy.entry_mode}_{cfg.management.mode}"
                Path(stem + ".md").write_text(text)
                bt.write_trades(res, Path(stem + "_trades.csv"))
                print(text)
                print(f"Saved {stem}.md and {stem}_trades.csv")
        elif a.cmd == "live":
            from . import live
            if not cfg.live_targets:
                raise ValueError("Add live_targets to config.yaml, e.g. [{symbol: XAUUSD, pair: H1-M5}]")
            targets = [(t["symbol"], *PAIRS[t["pair"]]) for t in cfg.live_targets]
            live.run(cfg, targets, a.live, a.poll, cfg.allow_real_account)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _compare(bars, symbol, htf, ltf, cfg) -> None:
    rows = []
    for em in ENTRY_MODES:
        for mm in MANAGEMENT_MODES:
            c = copy.deepcopy(cfg)
            c.strategy.entry_mode, c.management.mode = em, mm
            res = bt.run(bars, symbol, htf, ltf, c)
            s = bt.stats(res.trades, res.start_balance)
            rows.append((em, mm, s))
    lines = [f"# {symbol} {htf}-{ltf}: entry x management comparison", "",
             "| Entry | Management | Trades | Win rate | Avg R | Profit factor | Net % | Max DD % | Losing streak |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for em, mm, s in sorted(rows, key=lambda r: -(r[2].get("avg_r") or -9)):
        if not s.get("trades"):
            lines.append(f"| {em} | {mm} | 0 | | | | | | |")
            continue
        lines.append(f"| {em} | {mm} | {s['trades']} | {s['win_rate']:.1%} | {s['avg_r']:+.3f} | "
                     f"{s['profit_factor']:.2f} | {s['net_pct']:+.1f} | {s['max_drawdown_pct']:.1f} | "
                     f"{s['longest_losing_streak']} |")
    lines += ["", "Picking the best row from 12 tries flatters it. Confirm the winner on a different "
              "symbol or a later period before trusting it."]
    text = "\n".join(lines) + "\n"
    out = Path(f"reports/{symbol}_{htf}-{ltf}_compare.md")
    out.write_text(text)
    print(text)
    print(f"Saved {out}")


if __name__ == "__main__":
    sys.exit(main())
