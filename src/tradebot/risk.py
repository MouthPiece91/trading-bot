"""Position sizing and account protection. Shared by backtest and live trading."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .config import RiskConfig, SymbolSpec


def value_per_unit(spec: SymbolSpec, price: float) -> float:
    """Account-currency profit for a 1.0 price move on 1 lot."""
    if spec.conversion == "divide_by_price":
        return spec.contract_size / price
    return spec.contract_size


@dataclass
class Sizing:
    lots: float
    risk_amount: float
    risk_pct: float
    reason: str = ""


def size_position(equity: float, entry: float, sl: float, spec: SymbolSpec, r: RiskConfig,
                  value_override: float | None = None) -> Sizing:
    """Lots so that hitting the stop loses `risk_pct` of equity (rounded DOWN)."""
    if r.risk_pct > r.max_risk_pct or r.max_risk_pct > 3.0:
        return Sizing(0, 0, 0, "risk setting above the 3% hard cap")
    dist = abs(entry - sl)
    if dist <= 0:
        return Sizing(0, 0, 0, "stop equals entry")
    vpu = value_override if value_override is not None else value_per_unit(spec, entry)
    per_lot = dist * vpu + 2 * spec.commission_per_lot_side
    budget = equity * r.risk_pct / 100
    steps = math.floor(budget / per_lot / spec.volume_step + 1e-9)
    lots = min(round(steps * spec.volume_step, 8), spec.volume_max)
    if lots < spec.volume_min:
        return Sizing(0, 0, 0, f"stop too wide for {r.risk_pct}% risk at minimum lot {spec.volume_min}")
    actual = lots * per_lot
    pct = actual / equity * 100
    if pct > r.max_risk_pct + 1e-9:
        return Sizing(0, 0, 0, "would exceed the hard risk cap")
    return Sizing(lots, actual, pct)


class Guard:
    """Daily loss limit, max drawdown, trades per day, losing streaks."""

    def __init__(self, r: RiskConfig, starting_equity: float):
        self.r = r
        self.initial = starting_equity
        self.peak = starting_equity
        self.day: date | None = None
        self.day_start = starting_equity
        self.trades_today = 0
        self.losses_in_row = 0
        self.paused_day: date | None = None
        self.halted = False
        self.halt_reason = ""

    def new_bar(self, d: date, equity: float) -> None:
        if d != self.day:
            self.day, self.day_start, self.trades_today = d, equity, 0
        self.peak = max(self.peak, equity)
        ref = self.initial if self.r.drawdown_reference == "initial" else self.peak
        if equity <= ref * (1 - self.r.max_drawdown_pct / 100):
            self.halted, self.halt_reason = True, f"max drawdown {self.r.max_drawdown_pct}% reached"

    def can_trade(self, equity: float) -> tuple[bool, str]:
        if self.halted:
            return False, self.halt_reason
        if equity <= self.day_start * (1 - self.r.daily_loss_pct / 100):
            return False, "daily loss limit"
        if self.trades_today >= self.r.max_trades_per_day:
            return False, "max trades per day"
        if self.paused_day == self.day:
            return False, "losing streak pause"
        return True, ""

    def on_open(self) -> None:
        self.trades_today += 1

    def on_close(self, pnl: float) -> None:
        if pnl < 0:
            self.losses_in_row += 1
            if self.losses_in_row >= self.r.max_consecutive_losses:
                self.paused_day = self.day
                self.losses_in_row = 0
        elif pnl > 0:
            self.losses_in_row = 0
