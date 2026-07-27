# ESTrader A.I. — Albion Trading Desk
**Version:** 1.0.0 | **Port:** 5007 | **Instrument:** US500 (S&P 500 CFD, Capital.com) | **Status:** Paper Trading

The most architecturally significant new system on the desk. ESTrader is a **fork of USTrader (5004)**
that trades the **US500 CFD ~23 hours/day** (Sun 22:00 → Fri 21:00 UTC) and pilots **Merlin's Memory** —
Arthur's episodic recall of his own past decisions and outcomes.

It is **not** a port of the archived TrendSurferAI (SPY options) — that system was too old/incompatible.

## Four differences vs USTrader
1. **Extended session (23h US500), not NYSE-only.** Phases: `ASIAN` 22:00–07:00, `PRE_MARKET` 07:00–13:30,
   `US_SESSION` 13:30–21:00 (prime), `CLOSED` 21:00–22:00 (daily maintenance). Positions force-close at
   20:55 UTC before the break. Arthur receives the phase and **calibrates confidence** (US_SESSION highest,
   ASIAN lowest); it is **not** a hard gate.
2. **No Morgan.** No confidence tracker, hard block, warning/critical panels, or reset endpoint. Arthur is
   gated only by **Lancelot pre-checks** and the **daily-loss kill switch**.
3. **No Guinevere sentiment.** The **economic-calendar HARD BLOCK** (FOMC/NFP/CPI/GDP) is **kept** as a
   Lancelot risk control; only the news/sentiment framing is removed.
4. **Merlin's Memory (NEW).** See below.

Everything else is inherited from USTrader: Lancelot pre-checks, Arthur entry+exit, Stanley paper trader,
phantom logging with `fair_comparison`, ARTHUR_EXIT recovery logging, MAE/MFE logging, Excalibur
(Capital.com) connector, Profit Protection Ladder, three-process (dashboard / main / watchdog) architecture.

## Merlin's Memory (`merlin_memory_es.py`)
Regenerated **fresh from live CSVs at every Arthur consultation** and injected into his prompt as a
`TRADING MEMORY` section, capped at **600 tokens** (priority: patterns > trades > phantom):
- **Type 1 — recent completed trades** (`es_trades.csv`, last 10) + an auto-generated *lesson*
  (ladder win / stop / well-timed exit / premature-recovered / daily-SSL-flip).
- **Type 2 — recent FAIR stay-outs** (`es_phantom_trades.csv`, last 10, `fair_comparison=TRUE` only — unfair
  rows are excluded so Arthur isn't taught the wrong lessons).
- **Type 3 — self-observed patterns** (n≥3 only): session timing, extended-entry stop rate, under-traded
  3-SSL-agree setups, ARTHUR_EXIT recover-vs-continue.

Each consult is logged to `es_memory_log.csv` (tokens / trades / phantoms / patterns / Arthur confidence) so
Gaius can later assess whether memory improves decision quality. A Percival notification fires once when a
pattern first reaches n=3. Benchmark fair-comparison links to **USBenchmark (5024)** — same instrument
control — until an ESBenchmark exists.

## Trade parameters (inherited from USTrader)
£1,000 starting capital · £0.667/pt · 30pt trailing stop · 45pt target · Profit Protection Ladder · bidirectional
(LONG when daily SSL BULL, SHORT when BEAR).

## Run
`start_estrader.bat` (dashboard + watchdog→main) · dashboard at http://localhost:5007
Individual: `start_estrader_dashboard.bat`, `start_estrader_watchdog.bat`. Nick restarts systems.

## NOT built yet (by design — see brief Part 5)
ESBenchmark (after 4+ weeks data), ESHybrid (after signal quality proven), Merlin's Memory on other systems
(ESTrader is the pilot).
