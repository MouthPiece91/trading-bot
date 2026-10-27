"""Live / paper trading loop on MetaTrader 5.

Safety rules built in:
- Default is DRY RUN: signals and actions are logged, no orders are sent.
- With --live, a REAL-money account is refused unless config sets
  `allow_real_account: true`. Use a DEMO account first.
- Create a file named STOP in the bot folder to stop it at the next bar.
- Only touches orders/positions carrying this bot's magic number.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import mt5
from .config import Config
from .manage import OpenTrade, manage
from .risk import Guard, size_position
from .strategy import Engine

log = logging.getLogger("tradebot")


class Runner:
    def __init__(self, cfg: Config, symbol: str, htf: str, ltf: str, live: bool, state_dir: Path):
        cfg = cfg.for_symbol(symbol) if symbol in cfg.symbols else cfg
        self.cfg, self.symbol, self.htf, self.ltf, self.live = cfg, symbol, htf, ltf, live
        self.engine = Engine(cfg.strategy, htf, ltf)
        self.state_path = state_dir / f"state_{symbol}_{htf}-{ltf}.json"
        self.trades: dict[str, dict] = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.pending: dict[int, int] = {}  # order ticket -> bars waited
        self.last_time: datetime | None = None
        self.guard: Guard | None = None

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.trades, indent=2, default=str))

    def warm_up(self, bars: int = 5000) -> None:
        hist = mt5.closed_bars(self.symbol, self.ltf, bars)
        for b in hist:
            self.engine.on_bar(b)  # build context only; old signals are ignored
        self.last_time = hist[-1].time if hist else None
        start = self.cfg.risk.initial_balance or mt5.equity()
        self.guard = Guard(self.cfg.risk, start)
        log.info("%s %s-%s warmed up on %d bars (last %s)", self.symbol, self.htf, self.ltf, len(hist), self.last_time)

    def poll(self) -> None:
        for b in mt5.closed_bars(self.symbol, self.ltf, 5):
            if self.last_time is None or b.time > self.last_time:
                self.last_time = b.time
                self.on_bar(b)

    def on_bar(self, b) -> None:
        eq = mt5.equity()
        self.guard.new_bar(b.time.date(), eq)
        self._manage(b)
        self._expire_pending()
        sig = self.engine.on_bar(b)
        if not sig:
            return
        tag = "LIVE" if self.live else "DRY RUN"
        side = "BUY" if sig.direction > 0 else "SELL"
        if mt5.my_positions(self.symbol) or mt5.my_orders(self.symbol):
            log.info("[%s] %s %s signal skipped: already in a trade", tag, self.symbol, side)
            return
        ok, why = self.guard.can_trade(eq)
        if not ok:
            log.info("[%s] %s %s signal skipped: %s", tag, self.symbol, side, why)
            return
        sizing = size_position(eq, sig.entry, sig.sl, mt5.spec(self.symbol), self.cfg.risk,
                               value_override=mt5.value_per_unit(self.symbol))
        if sizing.lots <= 0:
            log.info("[%s] %s %s signal skipped: %s", tag, self.symbol, side, sizing.reason)
            return
        log.info("[%s] %s %s %s entry %.5f SL %.5f TP %.5f (R:R %.1f) lots %.2f risk %.2f%% | %s",
                 tag, self.symbol, side, sig.order, sig.entry, sig.sl, sig.tp, sig.rr, sizing.lots,
                 sizing.risk_pct, sig.reasons)
        if not self.live:
            return
        ticket = mt5.place(self.symbol, sig.direction, sig.order, sig.entry, sig.sl, sig.tp, sizing.lots,
                           f"tb {sig.reasons.get('pattern') or 'zone'}")
        if sig.order == "limit":
            self.pending[ticket] = 0
        self.guard.on_open()

    def _expire_pending(self) -> None:
        for o in mt5.my_orders(self.symbol):
            waited = self.pending.get(o.ticket, 0) + 1
            self.pending[o.ticket] = waited
            if waited > self.cfg.strategy.limit_expiry and self.live:
                mt5.cancel(o.ticket)
                self.pending.pop(o.ticket, None)
                log.info("[LIVE] %s limit order %s expired and was cancelled", self.symbol, o.ticket)

    def _manage(self, b) -> None:
        open_now = {str(p.ticket): p for p in mt5.my_positions(self.symbol)}
        for tk in list(self.trades):
            if tk not in open_now:  # closed by stop/target since the last bar
                self.trades.pop(tk)
                pnl = mt5.closed_pnl(int(tk))
                self.guard.on_close(pnl)
                log.info("%s ticket %s closed, result %.2f", self.symbol, tk, pnl)
        for tk, p in open_now.items():
            if tk not in self.trades:
                d = 1 if p.type == 0 else -1
                spec = mt5.spec(self.symbol)
                cost = spec.default_spread_points * spec.point  # break-even covers the spread
                self.trades[tk] = asdict(OpenTrade(d, p.price_open, p.sl, p.tp, p.volume, p.sl, cost))
            t = OpenTrade(**self.trades[tk])
            act = manage(t, b, self.engine.ltf_atr.value, self.cfg.management)
            if act.close_fraction and self.live:
                spec = mt5.spec(self.symbol)
                part = round(p.volume * act.close_fraction / spec.volume_step) * spec.volume_step
                if part >= spec.volume_min and p.volume - part >= spec.volume_min:
                    mt5.close_partial(p.ticket, self.symbol, t.direction, part)
                    t.lots = round(p.volume - part, 8)
            if act.new_sl is not None:
                if self.live:
                    mt5.modify_sl(p.ticket, self.symbol, act.new_sl, t.tp)
                t.sl = act.new_sl
            if act.note:
                log.info("[%s] %s ticket %s: %s", "LIVE" if self.live else "DRY RUN", self.symbol, tk, act.note)
            self.trades[tk] = asdict(t)
        self._save()


def run(cfg: Config, targets: list[tuple[str, str, str]], live: bool, poll_seconds: int = 5,
        allow_real: bool = False, workdir: Path = Path(".")) -> None:
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(logs / "tradebot.log"), logging.StreamHandler()])
    mt5.connect()
    log.info("Connected: %s", mt5.account_summary())
    if live and not mt5.is_demo() and not allow_real:
        raise SystemExit("Refusing to trade a REAL account. Test on DEMO first, then set "
                         "allow_real_account: true in config.yaml when you are ready.")
    runners = [Runner(cfg, s, h, l, live, logs) for s, h, l in targets]
    for r in runners:
        r.warm_up()
    log.info("Running %s. Create a file named STOP here to stop.", "LIVE" if live else "DRY RUN")
    try:
        while not (workdir / "STOP").exists():
            for r in runners:
                try:
                    r.poll()
                except Exception as exc:  # keep running; one symbol's error must not stop the others
                    log.exception("%s error: %s", r.symbol, exc)
            time.sleep(poll_seconds)
        log.info("STOP file found. Bot stopped; open trades keep their stop loss and take profit.")
    finally:
        mt5.shutdown()
