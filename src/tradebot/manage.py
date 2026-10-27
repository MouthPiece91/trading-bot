"""Trade management rules (break-even, partial close, trailing), shared by backtest and live.

Checked once per closed lower-timeframe bar. A change decided on a bar applies from
the NEXT bar, so the backtest never assumes it acted inside a bar it could not see.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bars import Bar
from .config import ManagementConfig


@dataclass
class OpenTrade:
    direction: int
    entry: float
    sl: float
    tp: float
    lots: float
    initial_sl: float
    cost_per_unit: float = 0.0   # spread + commission expressed in price, for true break-even
    be_done: bool = False
    partial_done: bool = False


@dataclass
class Action:
    new_sl: float | None = None
    close_fraction: float = 0.0
    note: str = ""


def manage(t: OpenTrade, b: Bar, atr: float | None, m: ManagementConfig) -> Action:
    if m.mode == "none":
        return Action()
    d = t.direction
    trigger = t.entry + d * m.be_trigger * abs(t.tp - t.entry)
    best = b.high if d > 0 else b.low
    reached = (best >= trigger) if d > 0 else (best <= trigger)
    act = Action()
    if reached and not t.be_done:
        be = t.entry + d * t.cost_per_unit
        if (be - t.sl) * d > 0:
            act.new_sl = be
            act.note = "stop to break-even"
        t.be_done = True
        if m.mode == "breakeven_partial" and not t.partial_done:
            act.close_fraction = m.partial_pct
            t.partial_done = True
            act.note += f", closed {int(m.partial_pct * 100)}%"
    if m.mode == "trail" and t.be_done and atr:
        trail = b.close - d * m.trail_atr * atr
        current = act.new_sl if act.new_sl is not None else t.sl
        if (trail - current) * d > 0:
            act.new_sl = trail
            act.note = "trailing stop"
    return act
