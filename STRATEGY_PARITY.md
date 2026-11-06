# STRATEGY_PARITY.md (v2)

Source of truth: *ICT Power Of Three | Flux Charts* (Pine v5). Port: `po3_strategy.py`. **v2 did not change any strategy logic.**

## Status - read this first
| Claim | Status |
|---|---|
| Every trading-relevant Pine statement is ported in the same order (incl. cascading `if` blocks) | re-audited line by line in v2: **no logic defect found** |
| Engine == independent transcription of the script on identical OHLC | **verified locally** (24 input combinations × 6 random series + window test + incremental/persisted test) |
| Engine == TradingView chart output | **NOT DEMONSTRATED** (no TradingView export available). Do not assume 100 % parity. |
| Tool to demonstrate it | `tools/compare_tradingview.py` (see below) |

### What the local cross-check is - and is not
`tests/reference_pine.py` is a second, differently-structured transcription of the Pine source (whole-series arrays, Pine field names, `na`=None,
`PO3List`). `tests/test_parity_reference.py` runs both on synthetic regime data (calm ranges + bursts so Long/Short, TP, SL and same-bar TP+SL occur) for
Small/Short/Big Manipulation × Wick/Close × Dynamic/Fixed × Normal/Lowest risk, and requires identical (type, candle, entry, SL, TP, exit time).
It proves the production engine (incl. its persisted/incremental form) has no porting slip relative to *my reading* of the script. It cannot detect a
misreading shared by both transcriptions, nor differences in data or ATR seeding versus TradingView.

## Ported elements
Inputs and derived values (40/11 bars, ATR mult 5/2, manipulation mult 0.6/1, SL mult 9.5/6/5/4/1.5, RR 0.86), `ta.atr(50)` (RMA seeded with SMA, `na` for the
first 49 bars), `ta.highest/lowest`, Wick/Close breakout, createNewPO3 rule, state machine Accumulation → Accumulation End → Manipulation → Distribution → Entry,
box ±0.5·ATR, bullish-first manipulation test, entry at the signal bar's close (bearish manipulation = Long), Dynamic and Fixed TP/SL, exits only from the bar
after entry, TP then SL checks (SL overrides TP on the same bar). Not ported (visual only): boxes, lines, labels, tables, alert plumbing.

## v2 changes that touch *data handling* (not logic)
* History/cache raised to 4800/5000 candles = `max_distance_to_last_bar`; the first run processes up to 4800 bars like TradingView's window.
* The forming candle is **no longer cached** (v1 could store a partial candle and later treat it as closed).
* State persisted between runs; re-initialised from history when settings change or the saved state predates the cache. Candles are never fabricated;
  fewer than 500 closed candles → instrument rejected; 500-4799 → processed with a warning (`history_complete=false`).

## Known, unresolved differences vs a TradingView chart
1. **State phase:** Pine's state depends on its first processed bar (`last_bar_index − 4800`) and vendor history; after a rebuild the scanner's phase can differ until a trade completes.
2. **Data vendor:** Twelve Data / OANDA candles differ slightly from TradingView's feed (OHLC, session boundaries) and can flip threshold comparisons.
3. **ATR seed** converges (α = 1/50) but small early differences exist if history is shorter than TradingView's.
4. Pine treats the realtime bar inside the 4800 window; the scanner only uses closed candles.
5. **Forming candle:** the script cannot signal on an unconfirmed bar; `EVALUATE_FORMING_CANDLE=true` is a deliberate, labelled deviation (default off).

## How to demonstrate parity (action for you)
Export ≥5000 bars of one symbol from TradingView (time,open,high,low,close CSV) and write `signals.csv` (`time,side`) from the Buy/Sell labels, then
`python tools/compare_tradingview.py ohlc.csv signals.csv` (add `--mode/--breakout/--tpsl/--risk` if inputs differ). Exit 0 = identical after the first 300 bars.
Send me any differences and I will trace them without altering the strategy.
