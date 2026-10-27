"""Candlestick patterns from The Candlestick Trading Bible, as exact rules.

Each detector looks at the bars ending at the most recent CLOSED bar and returns a
Pattern or None. `direction` is +1 (bullish) or -1 (bearish). `extreme` is the
price beyond which the pattern is invalid (used for the stop).
"""

from __future__ import annotations

from dataclasses import dataclass

from .bars import Bar


@dataclass(frozen=True)
class Pattern:
    name: str
    direction: int
    extreme: float  # lowest low (bullish) / highest high (bearish) of the pattern
    high: float     # range of the confirmation, for 50% entries
    low: float


@dataclass(frozen=True)
class PatternConfig:
    pin_wick_ratio: float = 2.0      # rejection wick >= 2 x body ("shadow twice the body")
    pin_body_zone: float = 1 / 3     # body sits in the top (bull) / bottom (bear) third
    doji_body_max: float = 0.10      # body <= 10% of range
    tweezer_tol: float = 0.05        # lows/highs within 5% of the bigger range
    star_small_body: float = 0.5     # middle star body <= 50% of first body
    min_range_atr: float = 0.3       # ignore tiny candles (noise) smaller than 0.3 ATR


def pin_bar(b: Bar, c: PatternConfig) -> Pattern | None:
    if b.range <= 0:
        return None
    body = max(b.body, b.range * 0.01)
    if b.lower_wick >= c.pin_wick_ratio * body and b.body_bottom >= b.high - b.range * c.pin_body_zone:
        return Pattern("pin_bar", +1, b.low, b.high, b.low)
    if b.upper_wick >= c.pin_wick_ratio * body and b.body_top <= b.low + b.range * c.pin_body_zone:
        return Pattern("pin_bar", -1, b.high, b.high, b.low)
    return None


def engulfing(prev: Bar, b: Bar) -> Pattern | None:
    if prev.body == 0 or b.body == 0:
        return None
    covers = b.body_top >= prev.body_top and b.body_bottom <= prev.body_bottom and b.body > prev.body
    if not covers:
        return None
    if b.bullish and prev.bearish:
        return Pattern("engulfing", +1, min(prev.low, b.low), b.high, min(prev.low, b.low))
    if b.bearish and prev.bullish:
        return Pattern("engulfing", -1, max(prev.high, b.high), max(prev.high, b.high), b.low)
    return None


def inside_bar_false_breakout(mother: Bar, inside: Bar, b: Bar) -> Pattern | None:
    """Mother bar, an inside bar, then a bar that breaks out and closes back inside the mother."""
    if not (inside.high <= mother.high and inside.low >= mother.low):
        return None
    if b.low < inside.low and b.low < mother.low and mother.low < b.close < mother.high:
        return Pattern("inside_bar_false_breakout", +1, b.low, b.high, b.low)
    if b.high > inside.high and b.high > mother.high and mother.low < b.close < mother.high:
        return Pattern("inside_bar_false_breakout", -1, b.high, b.high, b.low)
    return None


def star(a: Bar, m: Bar, b: Bar, c: PatternConfig) -> Pattern | None:
    if a.body == 0 or m.body > c.star_small_body * a.body:
        return None
    mid = (a.open + a.close) / 2
    if a.bearish and b.bullish and b.close > mid:
        lo = min(a.low, m.low, b.low)
        return Pattern("morning_star", +1, lo, b.high, lo)
    if a.bullish and b.bearish and b.close < mid:
        hi = max(a.high, m.high, b.high)
        return Pattern("evening_star", -1, hi, hi, b.low)
    return None


def tweezer(prev: Bar, b: Bar, c: PatternConfig) -> Pattern | None:
    tol = c.tweezer_tol * max(prev.range, b.range)
    if tol <= 0:
        return None
    if prev.bearish and b.bullish and abs(prev.low - b.low) <= tol:
        lo = min(prev.low, b.low)
        return Pattern("tweezer_bottom", +1, lo, b.high, lo)
    if prev.bullish and b.bearish and abs(prev.high - b.high) <= tol:
        hi = max(prev.high, b.high)
        return Pattern("tweezer_top", -1, hi, hi, b.low)
    return None


def directional_doji(b: Bar, c: PatternConfig) -> Pattern | None:
    if b.range <= 0 or b.body > c.doji_body_max * b.range:
        return None
    if b.lower_wick >= 0.66 * b.range:
        return Pattern("dragonfly_doji", +1, b.low, b.high, b.low)
    if b.upper_wick >= 0.66 * b.range:
        return Pattern("gravestone_doji", -1, b.high, b.high, b.low)
    return None


def detect(bars: list[Bar], c: PatternConfig, atr: float | None = None,
           enabled: tuple[str, ...] | None = None) -> list[Pattern]:
    """All patterns completing on the last bar in `bars`."""
    if not bars:
        return []
    b = bars[-1]
    if atr and b.range < c.min_range_atr * atr:
        return []
    found: list[Pattern | None] = [pin_bar(b, c), directional_doji(b, c)]
    if len(bars) >= 2:
        found += [engulfing(bars[-2], b), tweezer(bars[-2], b, c)]
    if len(bars) >= 3:
        found += [inside_bar_false_breakout(bars[-3], bars[-2], b), star(bars[-3], bars[-2], b, c)]
    out = [p for p in found if p]
    if enabled is not None:
        out = [p for p in out if p.name in enabled]
    return out
