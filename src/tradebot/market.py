"""Market context, built incrementally so the backtest and live trading share it.

- Indicators: EMA, ATR
- Structure: confirmed swing highs/lows and trend (HH/HL, LH/LL)
- Zones: order blocks and fair value gaps
- Liquidity: swing highs/lows, previous day high/low, session highs/lows, sweeps

Nothing here looks into the future: a swing is only recorded `swing_len` bars after
it forms, and a zone only after the candle that creates it has closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .bars import Bar


class EMA:
    def __init__(self, period: int):
        self.k = 2 / (period + 1)
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.k * (x - self.value)
        return self.value


class ATR:
    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self.prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, b: Bar) -> float | None:
        tr = b.range if self.prev_close is None else max(
            b.high - b.low, abs(b.high - self.prev_close), abs(b.low - self.prev_close))
        self.prev_close = b.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


@dataclass
class Swing:
    index: int
    time: datetime
    price: float
    kind: str  # "high" | "low"
    broken: bool = False


class Structure:
    """Swing points (fractals) and trend on one timeframe."""

    def __init__(self, swing_len: int = 3):
        self.n = swing_len
        self.bars: list[Bar] = []
        self.highs: list[Swing] = []
        self.lows: list[Swing] = []

    def update(self, b: Bar) -> list[Swing]:
        self.bars.append(b)
        i = len(self.bars) - 1 - self.n  # candidate now has n bars on both sides
        if i < self.n:
            return []
        c = self.bars[i]
        left = self.bars[i - self.n:i]
        right = self.bars[i + 1:i + 1 + self.n]
        new = []
        if all(c.high > x.high for x in left) and all(c.high >= x.high for x in right):
            s = Swing(i, c.time, c.high, "high")
            self.highs.append(s)
            new.append(s)
        if all(c.low < x.low for x in left) and all(c.low <= x.low for x in right):
            s = Swing(i, c.time, c.low, "low")
            self.lows.append(s)
            new.append(s)
        return new

    def trend(self) -> int:
        if len(self.highs) < 2 or len(self.lows) < 2:
            return 0
        hh = self.highs[-1].price > self.highs[-2].price
        hl = self.lows[-1].price > self.lows[-2].price
        lh = self.highs[-1].price < self.highs[-2].price
        ll = self.lows[-1].price < self.lows[-2].price
        if hh and hl:
            return 1
        if lh and ll:
            return -1
        return 0

    def last_impulse(self, direction: int) -> tuple[float, float] | None:
        """(start, end) of the latest swing leg in `direction`, for Fibonacci levels."""
        if not self.highs or not self.lows:
            return None
        h, l = self.highs[-1], self.lows[-1]
        if direction > 0 and l.index < h.index:
            return l.price, h.price
        if direction < 0 and h.index < l.index:
            return h.price, l.price
        return None


@dataclass
class Zone:
    kind: str        # "OB" | "FVG"
    direction: int   # +1 demand (buy), -1 supply (sell)
    top: float
    bottom: float
    created: datetime
    created_index: int
    reached_depth: bool = False
    mitigated: bool = False
    used: bool = False

    def depth_price(self, depth: float) -> float:
        h = self.top - self.bottom
        return self.top - depth * h if self.direction > 0 else self.bottom + depth * h

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def active(self) -> bool:
        return not (self.mitigated or self.used)


class ZoneTracker:
    """Order blocks and fair value gaps on the higher timeframe."""

    def __init__(self, structure: Structure, ob_lookback: int = 10, fvg_min_atr: float = 0.2,
                 max_age: int = 120, use_ob: bool = True, use_fvg: bool = True):
        self.s = structure
        self.ob_lookback = ob_lookback
        self.fvg_min_atr = fvg_min_atr
        self.max_age = max_age
        self.use_ob = use_ob
        self.use_fvg = use_fvg
        self.zones: list[Zone] = []

    def update(self, atr: float | None) -> None:
        bars = self.s.bars
        i = len(bars) - 1
        b = bars[i]
        # Order blocks: a close through the latest unbroken swing = break of structure.
        if self.use_ob:
            if self.s.highs and not self.s.highs[-1].broken and b.close > self.s.highs[-1].price:
                self.s.highs[-1].broken = True
                for j in range(i - 1, max(-1, i - 1 - self.ob_lookback), -1):
                    if bars[j].bearish:
                        self.zones.append(Zone("OB", +1, bars[j].high, bars[j].low, b.time, i))
                        break
            if self.s.lows and not self.s.lows[-1].broken and b.close < self.s.lows[-1].price:
                self.s.lows[-1].broken = True
                for j in range(i - 1, max(-1, i - 1 - self.ob_lookback), -1):
                    if bars[j].bullish:
                        self.zones.append(Zone("OB", -1, bars[j].high, bars[j].low, b.time, i))
                        break
        # Fair value gaps.
        if self.use_fvg and i >= 2 and atr:
            a, c = bars[i - 2], b
            if c.low > a.high and c.low - a.high >= self.fvg_min_atr * atr:
                self.zones.append(Zone("FVG", +1, c.low, a.high, b.time, i))
            if c.high < a.low and a.low - c.high >= self.fvg_min_atr * atr:
                self.zones.append(Zone("FVG", -1, a.low, c.high, b.time, i))
        self.zones = [z for z in self.zones if z.active and i - z.created_index <= self.max_age]

    def on_price(self, b: Bar, depth: float) -> None:
        """Called on every lower-timeframe bar: depth reached / mitigation."""
        for z in self.zones:
            if not z.active:
                continue
            if z.direction > 0:
                if b.low <= z.depth_price(depth):
                    z.reached_depth = True
                if b.close < z.bottom:
                    z.mitigated = True
            else:
                if b.high >= z.depth_price(depth):
                    z.reached_depth = True
                if b.close > z.top:
                    z.mitigated = True


@dataclass
class Level:
    price: float
    side: int     # +1 buy-side liquidity (highs), -1 sell-side liquidity (lows)
    source: str   # "swing", "PDH", "PDL", "asia_high", ...
    swept: bool = False


@dataclass
class Sweep:
    index: int        # LTF bar index
    direction: int    # +1 sell-side taken (bullish), -1 buy-side taken (bearish)
    level: Level
    extreme: float    # wick extreme of the sweeping bar


@dataclass
class Sessions:
    """Session windows in UTC hours [start, end). Server time = UTC + offset."""
    server_utc_offset: int = 2
    windows: dict = field(default_factory=lambda: {"asia": (0, 6), "london": (7, 11), "newyork": (12, 16)})

    def name_at(self, t: datetime) -> str | None:
        h = (t - timedelta(hours=self.server_utc_offset)).hour
        for name, (s, e) in self.windows.items():
            if s <= h < e:
                return name
        return None


class Liquidity:
    """Liquidity levels and sweeps, updated on lower-timeframe bars."""

    def __init__(self, sessions: Sessions | None, max_levels: int = 30):
        self.sessions = sessions
        self.max_levels = max_levels
        self.levels: list[Level] = []
        self.sweeps: list[Sweep] = []
        self._day: date | None = None
        self._day_hi = self._day_lo = None
        self._sess: str | None = None
        self._sess_hi = self._sess_lo = None

    def add(self, price: float, side: int, source: str) -> None:
        self.levels.append(Level(price, side, source))
        self.levels = [x for x in self.levels if not x.swept][-self.max_levels:]

    def _roll(self, b: Bar) -> None:
        d = b.time.date()
        if self._day is not None and d != self._day:
            self.add(self._day_hi, +1, "PDH")
            self.add(self._day_lo, -1, "PDL")
            self._day_hi = self._day_lo = None
        self._day = d
        self._day_hi = b.high if self._day_hi is None else max(self._day_hi, b.high)
        self._day_lo = b.low if self._day_lo is None else min(self._day_lo, b.low)
        if not self.sessions:
            return
        s = self.sessions.name_at(b.time)
        if s != self._sess and self._sess is not None:
            self.add(self._sess_hi, +1, f"{self._sess}_high")
            self.add(self._sess_lo, -1, f"{self._sess}_low")
            self._sess_hi = self._sess_lo = None
        self._sess = s
        if s is not None:
            self._sess_hi = b.high if self._sess_hi is None else max(self._sess_hi, b.high)
            self._sess_lo = b.low if self._sess_lo is None else min(self._sess_lo, b.low)

    def update(self, b: Bar, index: int) -> list[Sweep]:
        new = []
        for lv in self.levels:
            if lv.swept:
                continue
            if lv.side < 0 and b.low < lv.price < b.close:
                lv.swept = True
                new.append(Sweep(index, +1, lv, b.low))
            elif lv.side > 0 and b.high > lv.price > b.close:
                lv.swept = True
                new.append(Sweep(index, -1, lv, b.high))
            elif (lv.side < 0 and b.close < lv.price) or (lv.side > 0 and b.close > lv.price):
                lv.swept = True  # taken by a real breakout, not a sweep: no longer liquidity
        self.sweeps = (self.sweeps + new)[-50:]
        self._roll(b)  # this bar's day/session extremes become levels only later
        return new

    def targets(self, direction: int, beyond: float) -> list[Level]:
        """Unswept opposing liquidity beyond a price, nearest first."""
        if direction > 0:
            c = [x for x in self.levels if x.side > 0 and not x.swept and x.price > beyond]
            return sorted(c, key=lambda x: x.price)
        c = [x for x in self.levels if x.side < 0 and not x.swept and x.price < beyond]
        return sorted(c, key=lambda x: -x.price)
