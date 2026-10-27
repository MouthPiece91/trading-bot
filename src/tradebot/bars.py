"""Price bars: loading, and building higher-timeframe bars from lower ones.

Times are broker server time, stored as naive datetimes (MetaTrader convention).
Prices are BID prices, as MetaTrader stores them. `spread` is in price units.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
PAIRS = {"H4-M15": ("H4", "M15"), "H1-M5": ("H1", "M5")}


@dataclass(frozen=True)
class Bar:
    time: datetime  # bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0  # price units

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_top

    @property
    def lower_wick(self) -> float:
        return self.body_bottom - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


def _parse_time(date: str, time: str | None = None) -> datetime:
    s = f"{date} {time}" if time else date
    s = s.strip().replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised time: {s!r}")


def load_csv(path: str | Path, point: float = 0.0) -> list[Bar]:
    """Load bars from either:
    - this bot's own export: time,open,high,low,close,tick_volume,spread(points)
    - MetaTrader's History Center export: <DATE> <TIME> <OPEN> ... (tab separated)
    `point` converts MetaTrader's spread (in points) into price units.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.excel_tab if "\t" in text.splitlines()[0] else csv.excel
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    bars = []
    for r in rows:
        r = {k.strip("<> ").lower(): v for k, v in r.items() if k}
        t = _parse_time(r["date"], r.get("time")) if "date" in r else _parse_time(r["time"])
        vol = float(r.get("tick_volume") or r.get("tickvol") or r.get("vol") or r.get("volume") or 0)
        spread = float(r.get("spread") or 0) * point
        bars.append(Bar(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), vol, spread))
    bars.sort(key=lambda b: b.time)
    return bars


def write_csv(bars: list[Bar], path: str | Path, point: float) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread"])
        for b in bars:
            w.writerow([b.time.strftime("%Y-%m-%d %H:%M:%S"), b.open, b.high, b.low, b.close, b.volume,
                        round(b.spread / point) if point else 0])


def bucket(t: datetime, minutes: int) -> datetime:
    """Start of the higher-timeframe bar that contains time t (aligned to midnight)."""
    day = datetime(t.year, t.month, t.day)
    m = (t - day).seconds // 60
    return day + timedelta(minutes=(m // minutes) * minutes)


class Aggregator:
    """Builds higher-timeframe bars from lower-timeframe bars as they arrive.

    `push` returns the HTF bar that just COMPLETED (or None). A bar is only complete
    once a lower bar from the next period arrives, so the strategy never sees an
    unfinished higher-timeframe candle.
    """

    def __init__(self, minutes: int):
        self.minutes = minutes
        self.current: Bar | None = None

    def push(self, b: Bar) -> Bar | None:
        start = bucket(b.time, self.minutes)
        cur = self.current
        if cur is None:
            self.current = Bar(start, b.open, b.high, b.low, b.close, b.volume, b.spread)
            return None
        if start == cur.time:
            self.current = Bar(cur.time, cur.open, max(cur.high, b.high), min(cur.low, b.low), b.close,
                               cur.volume + b.volume, max(cur.spread, b.spread))
            return None
        self.current = Bar(start, b.open, b.high, b.low, b.close, b.volume, b.spread)
        return cur


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    agg = Aggregator(minutes)
    out = [done for b in bars if (done := agg.push(b))]
    if agg.current:
        out.append(agg.current)
    return out
