from datetime import date, datetime, timedelta
from statistics import mean

import pytest

from synthetic import walk
from tradebot import patterns as P
from tradebot.backtest import report, run, stats
from tradebot.bars import Aggregator, Bar, load_csv, resample, write_csv
from tradebot.config import Config, ManagementConfig, RiskConfig, SymbolSpec, load
from tradebot.manage import OpenTrade, manage
from tradebot.market import Liquidity, Structure, ZoneTracker
from tradebot.risk import Guard, size_position

T = datetime(2026, 1, 5)
C = P.PatternConfig(min_range_atr=0)


def bar(o, h, l, c, i=0, minutes=5):
    return Bar(T + timedelta(minutes=minutes * i), o, h, l, c)


# ---------------- candlestick patterns (book definitions) ---------------- #

def test_pin_bars():
    assert P.pin_bar(bar(10, 10.2, 8, 10.1), C).direction == 1      # hammer: long lower wick
    assert P.pin_bar(bar(10, 12, 9.9, 9.95), C).direction == -1     # shooting star
    assert P.pin_bar(bar(10, 11, 9, 10.9), C) is None               # big body: not a pin


def test_engulfing_needs_opposite_colours_and_full_body_cover():
    assert P.engulfing(bar(10, 10.1, 9.7, 9.8), bar(9.7, 10.3, 9.6, 10.2, 1)).direction == 1
    assert P.engulfing(bar(9.8, 10.1, 9.7, 10.0), bar(10.1, 10.2, 9.6, 9.7, 1)).direction == -1
    assert P.engulfing(bar(10, 10.1, 9.7, 9.8), bar(9.85, 10.0, 9.8, 9.95, 1)) is None


def test_inside_bar_false_breakout():
    mother, inside = bar(10, 11, 9, 10.5), bar(10.4, 10.6, 9.8, 10.2, 1)
    fake_down = bar(10.2, 10.4, 8.8, 9.6, 2)  # breaks below mother, closes back inside
    assert P.inside_bar_false_breakout(mother, inside, fake_down).direction == 1
    real_down = bar(10.2, 10.3, 8.5, 8.7, 2)
    assert P.inside_bar_false_breakout(mother, inside, real_down) is None


def test_morning_star_and_tweezer_and_doji():
    a, m, b = bar(11, 11.1, 9.9, 10), bar(10, 10.1, 9.8, 9.9, 1), bar(9.9, 10.9, 9.85, 10.8, 2)
    assert P.star(a, m, b, C).name == "morning_star"
    assert P.tweezer(bar(10, 10.1, 9, 9.2), bar(9.2, 10, 9.01, 9.9, 1), C).name == "tweezer_bottom"
    assert P.directional_doji(bar(10, 10.02, 9, 10.01), C).name == "dragonfly_doji"


def test_tiny_candles_ignored_as_noise():
    assert P.detect([bar(10, 10.2, 8, 10.1)], P.PatternConfig(), atr=10) == []
    assert P.detect([bar(10, 10.2, 8, 10.1)], P.PatternConfig(), atr=1)


# ---------------- bars and timeframes ---------------- #

def test_higher_timeframe_bar_only_released_when_complete():
    agg = Aggregator(60)
    out = [agg.push(bar(1, 2, 0.5, 1.5, i)) for i in range(12)]  # 12 x M5 = one hour
    assert all(x is None for x in out)
    done = agg.push(bar(1.5, 3, 1, 2, 12))
    assert done.time == T and done.high == 2 and done.low == 0.5


def test_csv_roundtrip(tmp_path):
    bars = walk(300, 1)
    write_csv(bars, tmp_path / "x.csv", 0.00001)
    back = load_csv(tmp_path / "x.csv", 0.00001)
    assert len(back) == 300 and abs(back[5].close - bars[5].close) < 1e-9
    assert len(resample(back, 60)) == 25


def test_mt5_history_center_format(tmp_path):
    f = tmp_path / "mt.csv"
    f.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                 "2026.01.05\t00:00:00\t1.1\t1.2\t1.0\t1.15\t10\t0\t12\n")
    (b,) = load_csv(f, 0.00001)
    assert b.time == datetime(2026, 1, 5) and abs(b.spread - 0.00012) < 1e-12


# ---------------- structure, zones, liquidity ---------------- #

def test_swing_only_confirmed_after_n_bars():
    s = Structure(2)
    prices = [1, 2, 5, 2, 1]
    found = [s.update(bar(p, p + 0.1, p - 0.1, p, i)) for i, p in enumerate(prices)]
    assert found[:4] == [[], [], [], []] and found[4][0].price == 5.1


def test_trend_from_swings():
    s = Structure(1)
    for i, p in enumerate([1, 3, 2, 4, 3, 5, 4]):  # higher highs and higher lows
        s.update(bar(p, p + 0.1, p - 0.1, p, i))
    assert s.trend() == 1


def test_fvg_and_depth_and_mitigation():
    s = Structure(2)
    z = ZoneTracker(s, use_ob=False)
    for i, b in enumerate([bar(1, 1.1, 0.9, 1.05), bar(1.05, 1.6, 1.0, 1.55, 1), bar(1.55, 1.7, 1.3, 1.65, 2)]):
        s.update(b)
        z.update(atr=0.1)
    (fvg,) = z.zones
    assert (fvg.bottom, fvg.top, fvg.direction) == (1.1, 1.3, 1)
    z.on_price(bar(1.4, 1.45, 1.25, 1.3, 3), 0.5)
    assert not fvg.reached_depth            # 1.25 is not yet 50% into 1.1-1.3
    z.on_price(bar(1.3, 1.35, 1.19, 1.25, 4), 0.5)
    assert fvg.reached_depth and fvg.active
    z.on_price(bar(1.2, 1.2, 1.0, 1.05, 5), 0.5)
    assert fvg.mitigated


def test_sweep_versus_breakout():
    liq = Liquidity(None)
    liq.add(1.0, -1, "swing")
    liq.add(2.0, +1, "swing")
    (sw,) = liq.update(bar(1.1, 1.2, 0.95, 1.05), 0)   # wick below 1.0, close back above
    assert sw.direction == 1 and sw.level.source == "swing"
    assert liq.update(bar(1.9, 2.3, 1.9, 2.2, 1), 1) == []  # closed above: breakout, not a sweep


# ---------------- risk ---------------- #

SPEC = SymbolSpec(contract_size=100000, point=0.00001)


def test_position_size_rounds_down_and_respects_risk():
    s = size_position(10000, 1.1000, 1.0980, SPEC, RiskConfig(risk_pct=1.0))
    assert s.lots == 0.5 and abs(s.risk_pct - 1.0) < 1e-9   # 20 pips x $10 x 0.5 lot = $100


def test_three_percent_hard_cap_cannot_be_configured():
    with pytest.raises(ValueError):
        load_cfg = Config(risk=RiskConfig(risk_pct=5, max_risk_pct=5))
        load_cfg.validate()


def test_skip_when_min_lot_too_risky():
    s = size_position(100, 1.1000, 1.0900, SPEC, RiskConfig(risk_pct=1.0))
    assert s.lots == 0 and "minimum lot" in s.reason


def test_divide_by_price_for_usd_base_pairs():
    jpy = SymbolSpec(contract_size=100000, point=0.001, conversion="divide_by_price")
    s = size_position(10000, 150.00, 149.80, jpy, RiskConfig(risk_pct=1.0))
    assert s.lots == 0.75  # 0.20 x 100000 / 150 = $133.3 per lot -> $100 = 0.75 lot


def test_guard_daily_loss_drawdown_and_streak():
    g = Guard(RiskConfig(daily_loss_pct=4, max_drawdown_pct=8, max_consecutive_losses=2), 10000)
    g.new_bar(date(2026, 1, 5), 10000)
    assert g.can_trade(9700)[0] and not g.can_trade(9590)[0]
    g.on_close(-1)
    g.on_close(-1)
    assert g.can_trade(10000) == (False, "losing streak pause")
    g.new_bar(date(2026, 1, 6), 10000)
    assert g.can_trade(10000)[0]
    g.new_bar(date(2026, 1, 7), 9150)
    ok, why = g.can_trade(9150)
    assert not ok and why.startswith("max drawdown")


# ---------------- management ---------------- #

def test_breakeven_partial_at_60_percent():
    t = OpenTrade(1, 100, 99, 105, 1.0, 99, cost_per_unit=0.02)
    a = manage(t, bar(101, 102.5, 100.9, 102), 0.5, ManagementConfig("breakeven_partial", 0.6, 0.25))
    assert a.close_fraction == 0 and a.new_sl is None          # 102.5 < 103 trigger
    a = manage(t, bar(102, 103.1, 101.9, 103), 0.5, ManagementConfig("breakeven_partial", 0.6, 0.25))
    assert a.new_sl == 100.02 and a.close_fraction == 0.25     # break-even covers costs
    assert manage(t, bar(103, 104, 102, 103.5), 0.5, ManagementConfig("breakeven_partial")).close_fraction == 0


def test_trailing_only_moves_in_favour():
    t = OpenTrade(-1, 100, 101, 95, 1.0, 101)
    m = ManagementConfig("trail", 0.6, 0, 1.0)
    a = manage(t, bar(98, 98.2, 96.9, 97), 0.5, m)
    assert a.new_sl == 97.5
    t.sl = a.new_sl
    assert manage(t, bar(97, 98.5, 96.8, 98.4), 0.5, m).new_sl is None


# ---------------- backtest integrity ---------------- #

def _cfg(**risk):
    r = dict(max_drawdown_pct=99, daily_loss_pct=99, max_trades_per_day=20, max_consecutive_losses=99)
    r.update(risk)
    return Config(symbols={"EURUSD": SymbolSpec(default_spread_points=0)}, risk=RiskConfig(**r))


def test_no_lookahead_trades_identical_on_shorter_history():
    bars = walk(20000, 3, spread=0.0)
    full = run(bars, "EURUSD", "H1", "M5", _cfg())
    cut = run(bars[:14000], "EURUSD", "H1", "M5", _cfg())
    end = bars[13999].time
    a = [(t.entry_time, t.entry, t.exit_time, t.exit) for t in full.trades if t.exit_time <= end]
    b = [(t.entry_time, t.entry, t.exit_time, t.exit) for t in cut.trades]
    assert a and a == b


def test_no_edge_on_random_data_without_costs():
    rs = []
    for seed in range(4):
        cfg = _cfg()
        cfg.management.mode = "none"
        rs += [t.r_multiple for t in run(walk(30000, seed, spread=0.0), "EURUSD", "H1", "M5", cfg).trades]
    assert len(rs) > 100 and abs(mean(rs)) < 0.35


def test_costs_reduce_results():
    bars0 = walk(30000, 5, spread=0.0)
    bars1 = [Bar(b.time, b.open, b.high, b.low, b.close, 0, 0.0002) for b in bars0]
    r0 = run(bars0, "EURUSD", "H1", "M5", _cfg())
    r1 = run(bars1, "EURUSD", "H1", "M5", _cfg())
    assert stats(r1.trades, 10000)["avg_r"] < stats(r0.trades, 10000)["avg_r"]


def test_report_and_guard_halt():
    res = run(walk(30000, 1), "EURUSD", "H1", "M5",
              Config(symbols={"EURUSD": SymbolSpec()}, risk=RiskConfig(max_drawdown_pct=3)))
    text = report(res, Config())
    assert "Verdict" in text
    assert min(e for _, e in res.equity_curve) > 10000 * (1 - 0.03) - 200  # halts near the limit
