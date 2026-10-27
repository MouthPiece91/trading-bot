"""Event-driven backtester.

Honest-by-design choices:
- Signals are made on a CLOSED bar and filled on the NEXT bar (market at its open,
  limits only if a later bar trades through the price).
- Bars are BID prices. Buys fill at the ask (bid + spread); sells exit at the ask.
- If a bar touches both stop and target, the STOP is assumed to hit first.
- Commission is charged per lot per side.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean

from .bars import Bar
from .config import Config
from .manage import OpenTrade, manage
from .risk import Guard, size_position, value_per_unit
from .strategy import Engine, Signal


@dataclass
class TradeRecord:
    symbol: str
    direction: str
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    sl: float
    tp: float
    lots: float
    pnl: float
    r_multiple: float
    risk_pct: float
    exit_reason: str
    pattern: str | None
    zone: str
    score: int
    equity_after: float


@dataclass
class Pending:
    sig: Signal
    created_index: int


@dataclass
class Result:
    symbol: str
    pair: str
    trades: list[TradeRecord]
    equity_curve: list[tuple[datetime, float]]
    skipped: dict = field(default_factory=dict)
    start_balance: float = 0.0
    bars: int = 0


def _spread(b: Bar, spec) -> float:
    return b.spread if b.spread > 0 else spec.default_spread_points * spec.point


def run(bars: list[Bar], symbol: str, htf: str, ltf: str, cfg: Config) -> Result:
    spec = cfg.spec(symbol)
    eng = Engine(cfg.strategy, htf, ltf)
    guard = Guard(cfg.risk, cfg.starting_balance)
    equity = cfg.starting_balance
    trades: list[TradeRecord] = []
    curve: list[tuple[datetime, float]] = []
    skipped: dict[str, int] = {}
    pending: Pending | None = None
    pos: OpenTrade | None = None
    pos_meta: dict = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def close(part_lots: float, price: float, when: datetime, reason: str) -> float:
        nonlocal equity
        vpu = value_per_unit(spec, price)
        pnl = (price - pos.entry) * pos.direction * vpu * part_lots - 2 * spec.commission_per_lot_side * part_lots
        equity += pnl
        return pnl

    for idx, b in enumerate(bars):
        sp = _spread(b, spec)
        guard.new_bar(b.time.date(), equity)

        # 1) fill pending orders with this bar
        if pending and pos is None:
            s = pending.sig
            age = idx - pending.created_index
            fill = None
            if s.order == "market":
                fill = b.open + (sp if s.direction > 0 else 0)
            elif s.direction > 0 and b.low + sp <= s.entry:
                fill = min(b.open + sp, s.entry)
            elif s.direction < 0 and b.high >= s.entry:
                fill = max(b.open, s.entry)
            if fill is not None:
                ok, why = guard.can_trade(equity)
                sizing = size_position(equity, fill, s.sl, spec, cfg.risk)
                if not ok:
                    skip(why)
                elif sizing.lots <= 0:
                    skip(sizing.reason)
                elif (fill - s.sl) * s.direction <= 0 or (s.tp - fill) * s.direction <= 0:
                    skip("price moved past stop or target before fill")
                else:
                    cost = sp + 2 * spec.commission_per_lot_side / value_per_unit(spec, fill)
                    pos = OpenTrade(s.direction, fill, s.sl, s.tp, sizing.lots, s.sl, cost)
                    pos_meta = {"sig": s, "entry_time": b.time, "risk_amt": sizing.risk_amount,
                                "risk_pct": sizing.risk_pct, "lots0": sizing.lots, "pnl": 0.0}
                    guard.on_open()
                pending = None
            elif age > s.expiry_bars:
                skip("limit order expired")
                pending = None

        # 2) stops / targets on this bar (stop first if both)
        if pos is not None:
            d = pos.direction
            lo = b.low + (sp if d < 0 else 0)
            hi = b.high + (sp if d < 0 else 0)
            hit_sl = lo <= pos.sl if d > 0 else hi >= pos.sl
            hit_tp = hi >= pos.tp if d > 0 else lo <= pos.tp
            exit_px = reason = None
            if hit_sl:
                gap = b.open + (sp if d < 0 else 0)
                exit_px = min(gap, pos.sl) if d > 0 else max(gap, pos.sl)
                reason = "stop" if pos.sl == pos.initial_sl else ("break-even/trail stop")
            elif hit_tp:
                exit_px, reason = pos.tp, "target"
            if exit_px is not None:
                pos_meta["pnl"] += close(pos.lots, exit_px, b.time, reason)
                s = pos_meta["sig"]
                pnl = pos_meta["pnl"]
                trades.append(TradeRecord(
                    symbol, "BUY" if d > 0 else "SELL", s.time, pos_meta["entry_time"], b.time,
                    round(pos.entry, 6), round(exit_px, 6), round(pos.initial_sl, 6), round(pos.tp, 6),
                    pos_meta["lots0"], round(pnl, 2), round(pnl / pos_meta["risk_amt"], 3),
                    round(pos_meta["risk_pct"], 3), reason, s.reasons.get("pattern"), s.reasons.get("zone", ""),
                    s.score, round(equity, 2)))
                guard.on_close(pnl)
                pos = None
            else:
                act = manage(pos, b, eng.ltf_atr.value, cfg.management)
                if act.close_fraction:
                    part = round(pos.lots * act.close_fraction / spec.volume_step) * spec.volume_step
                    if part >= spec.volume_min and pos.lots - part >= spec.volume_min:
                        trig = pos.entry + d * cfg.management.be_trigger * abs(pos.tp - pos.entry)
                        pos_meta["pnl"] += close(part, trig, b.time, "partial")
                        pos.lots = round(pos.lots - part, 8)
                if act.new_sl is not None:
                    pos.sl = act.new_sl

        # 3) the strategy sees the closed bar
        sig = eng.on_bar(b)
        if sig and pos is None and pending is None:
            pending = Pending(sig, idx)
        elif sig:
            skip("already in a trade")
        curve.append((b.time, equity))

    return Result(symbol, f"{htf}-{ltf}", trades, curve, skipped, cfg.starting_balance, len(bars))


# --------------------------------------------------------------------------- #
# Statistics and reports
# --------------------------------------------------------------------------- #

def stats(trades: list[TradeRecord], start: float, curve: list[tuple[datetime, float]] | None = None) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    streak = longest = 0
    for t in trades:
        streak = streak + 1 if t.pnl < 0 else 0
        longest = max(longest, streak)
    eq = [start] + [t.equity_after for t in trades]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_r": mean(rs),
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "net_pct": (eq[-1] - start) / start * 100,
        "max_drawdown_pct": mdd * 100,
        "longest_losing_streak": longest,
        "avg_win_r": mean([t.r_multiple for t in wins]) if wins else 0.0,
        "avg_loss_r": mean([t.r_multiple for t in losses]) if losses else 0.0,
    }


def monthly(trades: list[TradeRecord], start: float) -> list[tuple[str, float]]:
    out: dict[str, float] = {}
    eq_before = start
    month_start: dict[str, float] = {}
    for t in trades:
        k = t.exit_time.strftime("%Y-%m")
        month_start.setdefault(k, eq_before)
        out[k] = t.equity_after
        eq_before = t.equity_after
    return [(k, (out[k] - month_start[k]) / month_start[k] * 100) for k in out]


def _fmt(s: dict) -> list[str]:
    if not s.get("trades"):
        return ["No trades."]
    return [
        f"| Trades | {s['trades']} |",
        f"| Win rate | {s['win_rate']:.1%} |",
        f"| Average R per trade (expectancy) | {s['avg_r']:+.3f} R |",
        f"| Average win / loss | {s['avg_win_r']:+.2f} R / {s['avg_loss_r']:+.2f} R |",
        f"| Profit factor | {s['profit_factor']:.2f} |",
        f"| Net return | {s['net_pct']:+.1f}% |",
        f"| Max drawdown | {s['max_drawdown_pct']:.1f}% |",
        f"| Longest losing streak | {s['longest_losing_streak']} |",
    ]


def report(res: Result, cfg: Config) -> str:
    s = stats(res.trades, res.start_balance)
    half = len(res.trades) // 2
    first, second = res.trades[:half], res.trades[half:]
    s1 = stats(first, res.start_balance)
    s2 = stats(second, first[-1].equity_after if first else res.start_balance)
    st, m, r = cfg.strategy, cfg.management, cfg.risk
    lines = [
        f"# Backtest: {res.symbol} {res.pair}",
        "",
        f"Bars: {res.bars:,}. Entry: `{st.entry_mode}`. Management: `{m.mode}`. "
        f"Risk: {r.risk_pct}% per trade ({r.account_type}). Sweep required: {st.require_sweep}. "
        f"Min R:R {st.min_rr}.",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        *_fmt(s),
        "",
        "## Is it stable? First half vs second half of the trades",
        "",
        "| | First half | Second half |",
        "| --- | --- | --- |",
    ]
    if s1.get("trades") and s2.get("trades"):
        for key, label, f in [("trades", "Trades", "{}"), ("win_rate", "Win rate", "{:.1%}"),
                              ("avg_r", "Avg R", "{:+.3f}"), ("profit_factor", "Profit factor", "{:.2f}")]:
            lines.append(f"| {label} | {f.format(s1[key])} | {f.format(s2[key])} |")
    else:
        lines.append("| Not enough trades to split | | |")
    lines += ["", "## Monthly returns", "", "| Month | Return |", "| --- | --- |"]
    lines += [f"| {k} | {v:+.1f}% |" for k, v in monthly(res.trades, res.start_balance)]
    lines += ["", "## Setups not taken", "", "| Reason | Count |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in sorted(res.skipped.items(), key=lambda x: -x[1])] or ["| none | 0 |"]
    lines += ["", _verdict(s, s1, s2)]
    return "\n".join(lines) + "\n"


def _verdict(s: dict, s1: dict, s2: dict) -> str:
    if not s.get("trades"):
        return "**Verdict:** no trades. Loosen one rule at a time or use more data."
    if s["trades"] < 100:
        return (f"**Verdict:** only {s['trades']} trades; too few to judge. Add more data, symbols or "
                f"timeframe pairs before trusting any number above.")
    both = s1.get("avg_r", -1) > 0 and s2.get("avg_r", -1) > 0
    if s["avg_r"] > 0 and s["profit_factor"] > 1.3 and both:
        return ("**Verdict:** positive after costs and in both halves. Next step: paper-trade it live "
                "(dry run) for several weeks and compare with these numbers.")
    return ("**Verdict:** not proven. No real-money trading on these settings. "
            "Change one rule at a time and re-test.")


def write_trades(res: Result, path: Path) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        if res.trades:
            w.writerow(asdict(res.trades[0]).keys())
            for t in res.trades:
                w.writerow(asdict(t).values())
