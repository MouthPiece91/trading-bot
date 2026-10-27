from types import SimpleNamespace

import pytest

from synthetic import walk
from tradebot import live, mt5
from tradebot.bars import write_csv
from tradebot.cli import main
from tradebot.config import Config, SymbolSpec
from tradebot.strategy import Signal


class FakeMT5:
    def __init__(self, bars, demo=True):
        self.bars, self.demo, self.placed, self.pos = bars, demo, [], []
        self.cursor = 3000

    def install(self, mp):
        mp.setattr(mt5, "closed_bars", lambda s, tf, n: self.bars[max(0, self.cursor - n):self.cursor])
        mp.setattr(mt5, "equity", lambda: 10000.0)
        mp.setattr(mt5, "spec", lambda s: SymbolSpec())
        mp.setattr(mt5, "value_per_unit", lambda s: 100000.0)
        mp.setattr(mt5, "my_positions", lambda s: self.pos)
        mp.setattr(mt5, "my_orders", lambda s: [])
        mp.setattr(mt5, "place", lambda *a: self.placed.append(a) or 1)
        mp.setattr(mt5, "connect", lambda *a, **k: None)
        mp.setattr(mt5, "shutdown", lambda: None)
        mp.setattr(mt5, "is_demo", lambda: self.demo)
        mp.setattr(mt5, "account_summary", lambda: "fake")
        mp.setattr(mt5, "closed_pnl", lambda t: -100.0)


def _runner(tmp_path, monkeypatch, is_live):
    fake = FakeMT5(walk(6000, 2))
    fake.install(monkeypatch)
    r = live.Runner(Config(symbols={"EURUSD": SymbolSpec()}), "EURUSD", "H1", "M5", is_live, tmp_path)
    r.warm_up(3000)
    sig = Signal(fake.bars[3000].time, 1, "market", 1.1000, 1.0980, 1.1060, 1, 1, {"pattern": "pin_bar"})
    monkeypatch.setattr(r.engine, "on_bar", lambda b: sig)
    fake.cursor = 3001
    r.poll()
    return fake


def test_dry_run_never_sends_orders(tmp_path, monkeypatch):
    assert _runner(tmp_path, monkeypatch, is_live=False).placed == []


def test_live_sends_sized_order(tmp_path, monkeypatch):
    fake = _runner(tmp_path, monkeypatch, is_live=True)
    (args,) = fake.placed
    symbol, direction, order, entry, sl, tp, lots = args[:7]
    assert (symbol, direction, order, lots) == ("EURUSD", 1, "market", 0.5)


def test_real_account_refused_by_default(tmp_path, monkeypatch):
    fake = FakeMT5(walk(100, 1), demo=False)
    fake.install(monkeypatch)
    with pytest.raises(SystemExit):
        live.run(Config(), [("EURUSD", "H1", "M5")], live=True, workdir=tmp_path)


def test_cli_backtest_and_compare(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    write_csv(walk(15000, 4), tmp_path / "data" / "EURUSD_M5.csv", 0.00001)
    cfgfile = tmp_path / "c.yaml"
    cfgfile.write_text("symbols:\n  EURUSD: {contract_size: 100000, point: 0.00001}\n"
                       "risk: {max_drawdown_pct: 50, daily_loss_pct: 50}\n")
    assert main(["--config", str(cfgfile), "backtest", "--symbol", "EURUSD", "--pair", "H1-M5"]) == 0
    assert list((tmp_path / "reports").glob("*_trades.csv"))
    assert main(["--config", str(cfgfile), "backtest", "--symbol", "EURUSD", "--compare"]) == 0
    assert "entry x management comparison" in capsys.readouterr().out
    assert main(["--config", str(cfgfile), "backtest", "--symbol", "GBPUSD"]) == 1  # no contract details


def test_closed_losses_feed_the_streak_pause(tmp_path, monkeypatch):
    fake = FakeMT5(walk(6000, 2))
    fake.install(monkeypatch)
    cfg = Config(symbols={"EURUSD": SymbolSpec()})
    cfg.risk.max_consecutive_losses = 1
    r = live.Runner(cfg, "EURUSD", "H1", "M5", True, tmp_path)
    r.warm_up(3000)
    r.trades["77"] = {"direction": 1, "entry": 1.1, "sl": 1.09, "tp": 1.12, "lots": 0.1, "initial_sl": 1.09,
                      "cost_per_unit": 0.0, "be_done": False, "partial_done": False}
    fake.cursor = 3001
    r.poll()
    assert "77" not in r.trades and r.guard.can_trade(10000) == (False, "losing streak pause")
