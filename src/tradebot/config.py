"""Settings. Defaults here match STRATEGY.md; config.yaml overrides them."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml

ENTRY_MODES = ("confirm_close", "confirm_50", "zone_limit")
MANAGEMENT_MODES = ("none", "breakeven", "breakeven_partial", "trail")
ALL_PATTERNS = ("pin_bar", "engulfing", "inside_bar_false_breakout", "morning_star", "evening_star",
                "tweezer_bottom", "tweezer_top", "dragonfly_doji", "gravestone_doji")


@dataclass
class StrategyConfig:
    swing_len_htf: int = 3
    swing_len_ltf: int = 2
    zone_depth: float = 0.5
    zone_max_age: int = 120
    ob_lookback: int = 10
    fvg_min_atr: float = 0.2
    use_order_blocks: bool = True
    use_fvgs: bool = True
    require_sweep: bool = True
    sweep_lookback: int = 12
    patterns: list = field(default_factory=lambda: list(ALL_PATTERNS))
    min_pattern_range_atr: float = 0.3
    min_confluence: int = 0          # extra points needed on top of the required factors
    fib_band: tuple = (0.5, 0.618)
    ema_period: int = 21
    ema_near_atr: float = 0.5
    use_sessions: bool = True        # turn off for 24/7 synthetic indices
    server_utc_offset: int = 2
    entry_mode: str = "confirm_close"
    limit_expiry: int = 6            # LTF bars a limit order stays open
    sl_buffer_atr: float = 0.1
    min_rr: float = 2.0
    tp_mode: str = "nearest"         # nearest | first_meeting_rr


@dataclass
class ManagementConfig:
    mode: str = "breakeven_partial"
    be_trigger: float = 0.6
    partial_pct: float = 0.25
    trail_atr: float = 1.0


@dataclass
class RiskConfig:
    account_type: str = "personal"   # personal | prop
    risk_pct: float = 1.0
    max_risk_pct: float = 3.0        # hard cap; the bot refuses anything above
    daily_loss_pct: float = 4.0
    max_drawdown_pct: float = 8.0
    drawdown_reference: str = "initial"  # initial | peak
    max_trades_per_day: int = 3
    max_consecutive_losses: int = 3  # pause for the rest of the day
    initial_balance: float = 0.0     # prop: the account's starting balance (0 = equity when bot starts)


@dataclass
class SymbolSpec:
    contract_size: float = 100000
    point: float = 0.00001
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    conversion: str = "direct"       # direct | divide_by_price (USDJPY, USDCAD, USDCHF)
    commission_per_lot_side: float = 0.0
    default_spread_points: float = 10
    sessions: bool = True            # false for 24/7 synthetic indices: no session liquidity


@dataclass
class Config:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    symbols: dict = field(default_factory=dict)
    starting_balance: float = 10000.0
    live_targets: list = field(default_factory=list)  # [{symbol: XAUUSD, pair: H1-M5}, ...]
    allow_real_account: bool = False

    def spec(self, symbol: str) -> SymbolSpec:
        raw = self.symbols.get(symbol)
        if raw is None:
            raise KeyError(f"No contract details for {symbol}. Add it under 'symbols:' in config.yaml.")
        return raw if isinstance(raw, SymbolSpec) else SymbolSpec(**raw)

    def for_symbol(self, symbol: str) -> "Config":
        """A copy with symbol-specific switches applied (e.g. no sessions for synthetics)."""
        import copy
        c = copy.deepcopy(self)
        if not self.spec(symbol).sessions:
            c.strategy.use_sessions = False
        return c

    def validate(self) -> None:
        s, m, r = self.strategy, self.management, self.risk
        if s.entry_mode not in ENTRY_MODES:
            raise ValueError(f"entry_mode must be one of {ENTRY_MODES}")
        if m.mode not in MANAGEMENT_MODES:
            raise ValueError(f"management mode must be one of {MANAGEMENT_MODES}")
        if not 0 < r.risk_pct <= r.max_risk_pct <= 3.0:
            raise ValueError("risk_pct must be > 0 and <= max_risk_pct, and max_risk_pct can never exceed 3%")
        if not 0 < s.zone_depth <= 1:
            raise ValueError("zone_depth must be between 0 and 1")
        bad = set(s.patterns) - set(ALL_PATTERNS)
        if bad:
            raise ValueError(f"unknown patterns: {bad}")


def _fill(dc, data: dict):
    known = {f.name: f for f in fields(dc)}
    for k, v in (data or {}).items():
        if k not in known:
            raise ValueError(f"unknown setting '{k}' in {type(dc).__name__}")
        cur = getattr(dc, k)
        if is_dataclass(cur):
            _fill(cur, v)
        else:
            setattr(dc, k, tuple(v) if isinstance(cur, tuple) else v)
    return dc


def load(path: str | Path | None) -> Config:
    cfg = Config()
    if path and Path(path).exists():
        _fill(cfg, yaml.safe_load(Path(path).read_text()) or {})
    cfg.validate()
    return cfg
