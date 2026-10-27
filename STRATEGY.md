# Strategy specification: Liquidity + Candlestick Confirmation (v1)

This turns Felix's discretionary process and *The Candlestick Trading Bible*
into rules a computer can follow and a backtest can judge. Every number below is a
setting in `config.yaml`, so it can be tested and changed without editing code.

The rule for this project: **nothing goes live until the backtest says it has an
edge after costs.**

---

## 1. The process in one picture

```
Higher timeframe (H4 or H1)          Lower timeframe (M15 or M5)
---------------------------          ---------------------------
1. Bias: trend from swings      ->   4. Price trades 50% into the POI zone
2. POI: unmitigated OB / FVG    ->   5. Liquidity swept (swing / session / PDH-PDL)
   in the bias direction        ->   6. Candlestick confirmation
3. Target: opposing liquidity   ->   7. Enter, stop beyond zone, target liquidity
                                     8. Manage: break-even, partials, trailing
```

Timeframe pairs: **H4 -> M15** and **H1 -> M5** (Felix's two setups).

---

## 2. Higher timeframe: where and which way

### 2.1 Bias (book: "The Market Structure", "Trend")
- Swing highs/lows = fractals: a high with `swing_len` lower highs on each side.
  A swing is only known `swing_len` bars after it forms, so the bot never uses it
  early (no look-ahead).
- **Bullish:** last two swing highs rising AND last two swing lows rising (HH + HL).
- **Bearish:** LH + LL.
- Anything else = **no trend -> no trade** in v1. The book: trade with the trend;
  stay out of choppy markets.

### 2.2 Point of interest (POI) zones (Felix: order blocks / FVGs)
- **Order block (bullish):** the last bearish candle before a displacement move up
  that breaks the most recent swing high. Zone = that candle's low to high.
  Bearish OB is the mirror.
- **Fair value gap (bullish):** 3-candle gap where candle 3's low is above
  candle 1's high. Zone = candle 1 high to candle 3 low.
- A zone is **unmitigated** until price closes through its far edge; then it is
  dead. Zones older than `zone_max_age` HTF bars are dropped.
- Only zones on the bias side are used: demand below price for longs, supply above
  for shorts.

### 2.3 Liquidity (Felix: swing highs/lows, session highs/lows)
- Swing highs/lows (from 2.1), previous day high/low (PDH/PDL), and Asia / London
  session highs/lows.
- **Sweep:** a candle trades beyond a liquidity level and *closes back* on the
  other side. (Book: "stop loss hunting", inside-bar false breakout.)
- Targets = the nearest *opposing* liquidity (for a long: nearest swing high,
  session high or PDH above entry).

---

## 3. Lower timeframe: when

### 3.1 The 50% rule (Felix: "50% into the zone")
The entry is only allowed after price has traded at least `zone_depth` (default
0.5) into the POI zone. For a demand zone `[bottom, top]` that means price must
touch `top - 0.5 * (top - bottom)`.

### 3.2 Liquidity sweep
Within `sweep_lookback` LTF bars before the trigger, price must have swept a
liquidity level in the trade's favour (for a long: taken out a low and closed back
above it). Setting `require_sweep: false` turns this off for testing.

### 3.3 Candlestick confirmation (book: Candlestick Patterns chapters)

| Pattern | Rule used by the bot | Book reference |
| --- | --- | --- |
| Pin bar (hammer / shooting star) | Rejection wick >= `pin_wick_ratio` (2.0) x body, body in the opposite third of the range | "Hammer", "Shooting star: shadow twice the body" |
| Engulfing | Body of bar 2 fully covers body of bar 1, opposite colours | "Engulfing bar", Nison's 3 criteria |
| Inside bar false breakout | Bar breaks the mother bar's high/low and closes back inside it | "Inside bar false breakout" |
| Morning / evening star | Big candle, small middle candle, third closes past the first body's midpoint | "Morning star", "Evening star" |
| Tweezer bottom / top | Two candles with lows (highs) within `tweezer_tol` of range, opposite colours | "Tweezers tops and bottoms" |
| Dragonfly / gravestone doji | Body <= 10% of range, wick on one side | "Dragonfly", "Gravestone" |

Plain doji and harami/inside bars alone are **not** entries (the book calls them
indecision; they need a breakout to mean anything).

### 3.4 Confluence score
Each setup gets points; it must reach `min_confluence` to trade.

| Factor | Points | Source |
| --- | --- | --- |
| HTF trend in trade direction | required | Book: trend is factor #1 |
| Price >= 50% into an unmitigated HTF POI | required | Felix |
| Candlestick confirmation on LTF | required | Book |
| Liquidity sweep before trigger | +1 (required if `require_sweep`) | Felix; book's stop hunting |
| POI overlaps HTF 50%-61.8% Fibonacci of the last impulse | +1 | Book: "50% and 61%" |
| Price near HTF 21 EMA | +1 | Book: "8 and 21 moving average" |
| During London or New York session (not for 24/7 synthetics) | +1 | Felix: session liquidity |

---

## 4. Orders

- **Entry modes** (backtest all three, keep the winner):
  - `confirm_close`: market entry at the close of the confirmation candle
    (book's "aggressive entry").
  - `confirm_50`: limit order at 50% of the confirmation candle's range, valid
    `limit_expiry` bars (book's "conservative entry").
  - `zone_limit`: limit order at the 50% level of the POI zone, no candle needed
    (pure sniper; for comparison).
- **Stop loss:** beyond the far edge of the POI zone or the sweep wick, whichever
  is further, plus `sl_buffer_atr` x ATR.
- **Take profit:** nearest opposing liquidity. If reward/risk < `min_rr` (2.0,
  book's minimum) the trade is skipped.

---

## 5. Trade management (Felix's rules, tested against each other)

`management.mode`:
- `none`: stop and target only.
- `breakeven`: at `be_trigger` (60%) of the way to TP, move stop to entry + costs.
- `breakeven_partial`: same, and close `partial_pct` (25%) of the position.
- `trail`: after `be_trigger`, trail the stop `trail_atr` x ATR behind price.

"Break-even" is entry **plus spread and commission**, so a break-even exit is not a
small loss.

---

## 6. Risk (agreed limits)

| Setting | Personal | Prop firm |
| --- | --- | --- |
| Risk per trade | 1-2% | 0.5-1% |
| Hard cap per trade (code refuses above) | 3% | 3% |
| Daily loss stop | configurable | set below the firm's rule (e.g. 4% if firm is 5%) |
| Max total drawdown stop | configurable | set below the firm's rule |
| Max trades per day | `max_trades_per_day` | same |
| Pause after consecutive losses | `max_consecutive_losses` | same |

Final risk numbers will be set from the backtest's longest losing streak, not from
feel.

The high-risk "full margin" mode is **not** in v1. When added it will run on a
separate account with its own monthly budget and hard limits.

---

## 7. Where the book and this method disagree (be aware)

1. **Timeframes.** The book says patterns on small timeframes like 5-minute "will
   lose your money" because of noise, and recommends H1/H4/daily. Felix enters on
   M5 and M15. The backtest will show whether M5/M15 entries survive costs; spread
   matters much more on small timeframes.
2. **The book's numbers are not evidence.** Claims like "most profitable system",
   "$10 billion", or Bulkowski's 65% are not tied to a test we can reproduce. Our
   backtest is the evidence.
3. **Order blocks and FVGs** are not in the book; they come from Felix's liquidity
   method. The book's equivalent is "supply and demand zones" (strong move away
   from a zone on H4/daily).

---

## 8. What the backtest must show before live trading

- At least ~200 trades across symbols and both timeframe pairs.
- Positive expectancy (average R per trade > 0) **after** spread and commission.
- Profit factor > 1.3 and the edge present in both halves of the data
  (in-sample vs out-of-sample), not just one lucky period.
- Max drawdown and longest losing streak that the chosen risk % can survive within
  the prop firm's limits.

If it fails, we change one rule at a time and re-test, without curve-fitting to
one symbol.
