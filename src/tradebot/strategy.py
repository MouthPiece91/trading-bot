"""The strategy engine: feed it closed lower-timeframe bars, it returns trade signals.

The same Engine runs in the backtest and in live trading, so what you test is
exactly what trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .bars import TIMEFRAMES, Aggregator, Bar
from .config import StrategyConfig
from .market import ATR, EMA, Liquidity, Sessions, Structure, Sweep, Zone, ZoneTracker
from .patterns import Pattern, PatternConfig, detect


@dataclass
class Signal:
    time: datetime
    direction: int          # +1 buy, -1 sell
    order: str              # "market" | "limit"
    entry: float
    sl: float
    tp: float
    expiry_bars: int
    score: int
    reasons: dict = field(default_factory=dict)

    @property
    def rr(self) -> float:
        return abs(self.tp - self.entry) / abs(self.entry - self.sl)


class Engine:
    def __init__(self, cfg: StrategyConfig, htf: str, ltf: str):
        if TIMEFRAMES[htf] <= TIMEFRAMES[ltf]:
            raise ValueError("higher timeframe must be larger than the lower timeframe")
        self.cfg = cfg
        self.htf_name, self.ltf_name = htf, ltf
        self.agg = Aggregator(TIMEFRAMES[htf])
        self.htf = Structure(cfg.swing_len_htf)
        self.ltf = Structure(cfg.swing_len_ltf)
        self.htf_atr, self.ltf_atr = ATR(14), ATR(14)
        self.ema = EMA(cfg.ema_period)
        self.zones = ZoneTracker(self.htf, cfg.ob_lookback, cfg.fvg_min_atr, cfg.zone_max_age,
                                 cfg.use_order_blocks, cfg.use_fvgs)
        self.sessions = Sessions(cfg.server_utc_offset) if cfg.use_sessions else None
        self.liq = Liquidity(self.sessions)
        self.pcfg = PatternConfig(min_range_atr=cfg.min_pattern_range_atr)
        self.bars: list[Bar] = []
        self.i = -1

    # ----------------------------------------------------------------- #

    def _update(self, b: Bar) -> None:
        self.i += 1
        self.bars.append(b)
        if len(self.bars) > 500:
            self.bars = self.bars[-500:]
        self.ltf_atr.update(b)
        for s in self.ltf.update(b):
            self.liq.add(s.price, +1 if s.kind == "high" else -1, "ltf_swing")
        done = self.agg.push(b)
        if done:
            self.htf_atr.update(done)
            self.ema.update(done.close)
            for s in self.htf.update(done):
                self.liq.add(s.price, +1 if s.kind == "high" else -1, "htf_swing")
            self.zones.update(self.htf_atr.value)
        self.zones.on_price(b, self.cfg.zone_depth)
        self.liq.update(b, self.i)

    def _recent_sweep(self, direction: int) -> Sweep | None:
        for s in reversed(self.liq.sweeps):
            if s.direction == direction and self.i - s.index <= self.cfg.sweep_lookback:
                return s
        return None

    def _extras(self, z: Zone, direction: int, b: Bar, sweep: Sweep | None) -> dict:
        c = self.cfg
        out = {"sweep": sweep is not None}
        imp = self.htf.last_impulse(direction)
        if imp:
            start, end = imp
            lo_f, hi_f = c.fib_band
            a, bnd = end - hi_f * (end - start), end - lo_f * (end - start)
            band_lo, band_hi = min(a, bnd), max(a, bnd)
            out["fib"] = z.bottom <= band_hi and z.top >= band_lo
        else:
            out["fib"] = False
        ema, atr = self.ema.value, self.htf_atr.value
        out["ema21"] = bool(ema and atr and (z.bottom <= ema <= z.top or abs(z.mid - ema) <= c.ema_near_atr * atr))
        out["session"] = bool(self.sessions and self.sessions.name_at(b.time) in ("london", "newyork"))
        return out

    def _target(self, direction: int, entry: float, sl: float) -> float | None:
        risk = abs(entry - sl)
        for lv in self.liq.targets(direction, entry):
            rr = abs(lv.price - entry) / risk
            if self.cfg.tp_mode == "nearest":
                return lv.price if rr >= self.cfg.min_rr else None
            if rr >= self.cfg.min_rr:
                return lv.price
        return None

    # ----------------------------------------------------------------- #

    def on_bar(self, b: Bar) -> Signal | None:
        """Process one CLOSED lower-timeframe bar; maybe return a signal."""
        self._update(b)
        c = self.cfg
        if self.htf_atr.value is None or self.ltf_atr.value is None:
            return None
        trend = self.htf.trend()
        if trend == 0:
            return None
        zones = [z for z in self.zones.zones if z.active and z.direction == trend]
        if not zones:
            return None

        if c.entry_mode == "zone_limit":
            return self._zone_limit(b, trend, zones)

        pats = [p for p in detect(self.bars[-3:], self.pcfg, self.ltf_atr.value, tuple(c.patterns))
                if p.direction == trend]
        if not pats:
            return None
        sweep = self._recent_sweep(trend)
        if c.require_sweep and sweep is None:
            return None
        for z in zones:
            if not z.reached_depth:
                continue
            p = self._pattern_in_zone(pats, z, b)
            if not p:
                continue
            extras = self._extras(z, trend, b, sweep)
            score = sum(extras.values())
            if score < c.min_confluence:
                continue
            buf = c.sl_buffer_atr * self.ltf_atr.value
            stops = [z.bottom if trend > 0 else z.top, p.extreme] + ([sweep.extreme] if sweep else [])
            sl = (min(stops) - buf) if trend > 0 else (max(stops) + buf)
            if c.entry_mode == "confirm_close":
                order, entry, expiry = "market", b.close, 1
            else:
                order, entry, expiry = "limit", (p.high + p.low) / 2, c.limit_expiry
            if (entry - sl) * trend <= 0:
                continue
            tp = self._target(trend, entry, sl)
            if tp is None:
                continue
            z.used = True
            return Signal(b.time, trend, order, entry, sl, tp, expiry, score, {
                "zone": f"{z.kind} {z.bottom:.5g}-{z.top:.5g}", "pattern": p.name,
                "sweep": sweep.level.source if sweep else None, **extras})
        return None

    def _pattern_in_zone(self, pats: list[Pattern], z: Zone, b: Bar) -> Pattern | None:
        for p in pats:
            if z.direction > 0 and p.extreme <= z.top and b.close > z.bottom:
                return p
            if z.direction < 0 and p.extreme >= z.bottom and b.close < z.top:
                return p
        return None

    def _zone_limit(self, b: Bar, trend: int, zones: list[Zone]) -> Signal | None:
        c = self.cfg
        for z in zones:
            if z.reached_depth:
                continue
            outside = b.close > z.top if trend > 0 else b.close < z.bottom
            if not outside:
                continue
            entry = z.depth_price(c.zone_depth)
            buf = c.sl_buffer_atr * self.ltf_atr.value
            sl = z.bottom - buf if trend > 0 else z.top + buf
            tp = self._target(trend, entry, sl)
            if tp is None:
                continue
            z.used = True
            extras = self._extras(z, trend, b, None)
            life = c.zone_max_age * TIMEFRAMES[self.htf_name] // TIMEFRAMES[self.ltf_name]
            return Signal(b.time, trend, "limit", entry, sl, tp, life, sum(extras.values()),
                          {"zone": f"{z.kind} {z.bottom:.5g}-{z.top:.5g}", "pattern": None, **extras})
        return None
