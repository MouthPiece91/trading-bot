"""MetaTrader 5 connection (Windows only, needs the MT5 terminal installed and logged in).

Install with:  pip install -e ".[mt5]"
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .bars import Bar
from .config import SymbolSpec

try:  # the package only exists on Windows
    import MetaTrader5 as _mt5
except ImportError:  # pragma: no cover
    _mt5 = None

MAGIC = 913_001  # tags this bot's orders so it never touches your manual trades


def api():
    if _mt5 is None:
        raise RuntimeError("MetaTrader5 package not available. On Windows run: pip install -e \".[mt5]\"")
    return _mt5


def connect(login: int | None = None, password: str | None = None, server: str | None = None,
            path: str | None = None) -> None:
    m = api()
    kwargs = {k: v for k, v in {"login": login, "password": password, "server": server, "path": path}.items() if v}
    if not m.initialize(**kwargs):
        raise RuntimeError(f"Could not connect to MetaTrader 5: {m.last_error()}. Is the terminal open and logged in?")


def shutdown() -> None:
    if _mt5 is not None:
        _mt5.shutdown()


def timeframe(name: str):
    return getattr(api(), f"TIMEFRAME_{name}")


def _info(symbol: str):
    m = api()
    if not m.symbol_select(symbol, True):
        raise RuntimeError(f"Symbol {symbol} not found. Check the exact name in Market Watch (e.g. 'XAUUSD.m').")
    return m.symbol_info(symbol)


def _to_bars(rates, point: float) -> list[Bar]:
    return [Bar(datetime.fromtimestamp(int(r["time"]), timezone.utc).replace(tzinfo=None), float(r["open"]),
                float(r["high"]), float(r["low"]), float(r["close"]), float(r["tick_volume"]),
                float(r["spread"]) * point) for r in rates]


def history(symbol: str, tf: str, days: int) -> list[Bar]:
    info = _info(symbol)
    to = datetime.now(timezone.utc) + timedelta(days=1)
    rates = api().copy_rates_range(symbol, timeframe(tf), to - timedelta(days=days + 1), to)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No history returned for {symbol} {tf}: {api().last_error()}. "
                           f"Scroll the chart back in MT5 to download more, or raise 'Max bars in chart'.")
    return _to_bars(rates, info.point)


def closed_bars(symbol: str, tf: str, count: int) -> list[Bar]:
    """The last `count` CLOSED bars (position 0 is the still-forming bar, so start at 1)."""
    info = _info(symbol)
    rates = api().copy_rates_from_pos(symbol, timeframe(tf), 1, count)
    return _to_bars(rates, info.point) if rates is not None else []


def spec(symbol: str) -> SymbolSpec:
    i = _info(symbol)
    return SymbolSpec(contract_size=i.trade_contract_size, point=i.point, volume_min=i.volume_min,
                      volume_step=i.volume_step, volume_max=i.volume_max, default_spread_points=i.spread)


def value_per_unit(symbol: str) -> float:
    """Account-currency value of a 1.0 price move for 1 lot, straight from the broker."""
    i = _info(symbol)
    return i.trade_tick_value / i.trade_tick_size


def equity() -> float:
    return float(api().account_info().equity)


def account_summary() -> str:
    a = api().account_info()
    kind = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(a.trade_mode, str(a.trade_mode))
    return f"{a.login} on {a.server} ({kind}), balance {a.balance} {a.currency}, equity {a.equity}"


def is_demo() -> bool:
    return api().account_info().trade_mode == 0


# ------------------------------- orders ---------------------------------- #

def _filling(info):
    m = api()
    fm = info.filling_mode
    if fm & 1:
        return m.ORDER_FILLING_FOK
    if fm & 2:
        return m.ORDER_FILLING_IOC
    return m.ORDER_FILLING_RETURN


def _send(req: dict):
    m = api()
    res = m.order_send(req)
    if res is None or res.retcode not in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED):
        code = getattr(res, "retcode", None)
        raise RuntimeError(f"Order rejected (retcode {code}): {getattr(res, 'comment', m.last_error())}")
    return res


def place(symbol: str, direction: int, order: str, price: float, sl: float, tp: float, lots: float,
          comment: str = "tradebot") -> int:
    m = api()
    info = _info(symbol)
    tick = m.symbol_info_tick(symbol)
    digits = info.digits
    if order == "market":
        otype = m.ORDER_TYPE_BUY if direction > 0 else m.ORDER_TYPE_SELL
        price = tick.ask if direction > 0 else tick.bid
        action = m.TRADE_ACTION_DEAL
    else:
        otype = m.ORDER_TYPE_BUY_LIMIT if direction > 0 else m.ORDER_TYPE_SELL_LIMIT
        action = m.TRADE_ACTION_PENDING
    req = {"action": action, "symbol": symbol, "volume": lots, "type": otype, "price": round(price, digits),
           "sl": round(sl, digits), "tp": round(tp, digits), "deviation": 20, "magic": MAGIC,
           "comment": comment[:31], "type_time": m.ORDER_TIME_GTC, "type_filling": _filling(info)}
    res = _send(req)
    return int(res.order)


def modify_sl(ticket: int, symbol: str, sl: float, tp: float) -> None:
    m = api()
    d = _info(symbol).digits
    _send({"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol,
           "sl": round(sl, d), "tp": round(tp, d), "magic": MAGIC})


def close_partial(ticket: int, symbol: str, direction: int, lots: float) -> None:
    m = api()
    info = _info(symbol)
    tick = m.symbol_info_tick(symbol)
    _send({"action": m.TRADE_ACTION_DEAL, "position": ticket, "symbol": symbol, "volume": lots,
           "type": m.ORDER_TYPE_SELL if direction > 0 else m.ORDER_TYPE_BUY,
           "price": tick.bid if direction > 0 else tick.ask, "deviation": 20, "magic": MAGIC,
           "type_filling": _filling(info)})


def cancel(order_ticket: int) -> None:
    m = api()
    _send({"action": m.TRADE_ACTION_REMOVE, "order": order_ticket, "magic": MAGIC})


def closed_pnl(position_ticket: int) -> float:
    """Net result of a closed position (profit + commission + swap) from deal history."""
    deals = api().history_deals_get(position=position_ticket) or []
    return float(sum(d.profit + d.commission + d.swap for d in deals))


def my_positions(symbol: str) -> list:
    return [p for p in (api().positions_get(symbol=symbol) or []) if p.magic == MAGIC]


def my_orders(symbol: str) -> list:
    return [o for o in (api().orders_get(symbol=symbol) or []) if o.magic == MAGIC]
