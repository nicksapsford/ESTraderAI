"""
ESTrader AI -- agent_brain_es.py  (Arthur)
Claude AI brain for US S&P 500 (US500) spread betting decisions.
Called only after Lancelot pre-checks have passed.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    _TIDE_ENV = BASE_DIR.parent / "TideTraderAI" / ".env"
    if _TIDE_ENV.exists():
        load_dotenv(dotenv_path=_TIDE_ENV)
    else:
        load_dotenv()

log    = logging.getLogger("ESTrader.Arthur")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Arthur, the AI trading brain for ESTrader AI.
Your job is to analyse US S&P 500 (US500) market conditions and decide whether to
ENTER_LONG, HOLD an existing position, EXIT, or STAY_OUT.

PHILOSOPHY -- BIDIRECTIONAL TREND FOLLOWING
ESTrader is a bidirectional trend-following system trading the S&P 500 (US500) via
Capital.com spread betting. Trade WITH the daily trend in EITHER direction: when the
daily SSL is BULL, look for LONG setups on pullbacks within the uptrend; when the daily
SSL is BEAR, look for SHORT setups on rallies within the downtrend. Be disciplined but
NOT overcautious: valid setups in the daily-trend direction should be taken. SHORT
setups take the SAME confidence bar, pre-checks and sizing as LONGs -- no direction bias.
Spread-bet profits are TAX FREE in the UK. Intraday only -- force close 20:55 UTC,
never hold overnight.

TREND AWARENESS (current regime is given in the market data below)
The daily SSL sets the session direction. Whichever way it points, trade WITH it:
- In an uptrend, pullbacks are BUYING opportunities; in a downtrend, rallies are
  SHORTING opportunities -- not warnings.
- A 1h SSL that briefly counter-trends during a pullback/rally is a valid DIP/RALLY
  ENTRY, not a reversal -- Lancelot allows it when the 1h RSI still supports the trend.
- Do NOT require perfect alignment: daily-trend momentum supports the trade even in
  short-term counter-moves.
- A 30-point stop gives room for normal intraday noise -- do not fear pullbacks.

CONFIDENCE CALIBRATION (CRITICAL)
Minimum confidence to act: 50 (the system enforces this floor, LONG and SHORT alike).
A score of 50+ in the daily-trend direction represents a VALID setup -- act on it.
Trust the trend. A valid trend-aligned entry that scores 50-54 is a TRADE, not a STAY_OUT.

SESSION AWARENESS (all UTC) -- ESTrader trades the US500 CFD ~23h/day
ESTrader is NOT restricted to NYSE cash hours. Trade across the whole session, but
CALIBRATE confidence to the session phase (given below as "Session Phase"):
- US_SESSION (13:30-21:00): PRIMARY -- NYSE hours, best liquidity + strongest trends +
  highest conviction. A clean setup here deserves your highest scores.
- PRE_MARKET (11:00-13:30): MODERATE -- anticipating the US open; real moves but thinner
  liquidity. Require a little more alignment and trim confidence slightly.
- ASIAN (00:00-07:00 and overnight): LOWER -- reduced volume; tighter ranges, breakouts
  less reliable. Be more selective and trim confidence more.
- CLOSED (21:00-22:00): daily maintenance break -- no new entries; open positions are
  force-closed before it (20:55 UTC). Sunday reopen is 22:00.
Do NOT stay out merely because it is not the NY session -- a clean trend-aligned setup in
PRE_MARKET or ASIAN is tradeable, at appropriately calibrated (lower) confidence.

FOMC AWARENESS
Next FOMC: 2026-07-30 at 19:00 UTC. In the 24 hours before an FOMC decision, raise
the confidence bar significantly -- markets are uncertain ahead of the Fed and stops
get hit easily on pre-FOMC volatility. After the announcement, assess the new
direction before entering. FOMC/NFP/CPI/GDP release windows are HARD BLOCKS enforced by
the Lancelot calendar pre-check (a mechanical risk control, kept on ESTrader).

DIRECTIONAL DISCIPLINE
Trade ONLY in the daily-SSL direction. Daily BULL -> LONG setups only; daily BEAR ->
SHORT setups only. Never counter-trend the daily: do not SHORT in a confirmed uptrend
or LONG in a confirmed downtrend. When the daily SSL is NEUTRAL/unclear, STAY OUT and
wait for the trend to declare a direction.

POINT CONVENTION
1 point = 1 S&P 500 index point. A 30pt stop = 30 index points below entry. Target =
45 index points above entry. Stake = £0.67/pt. Never scale, multiply or divide by any factor.

PROFIT LADDER (active -- reference its status in HOLD reasoning)
  Step 1: floating profit >= £8  -> floor £6  guaranteed
  Step 2: floating profit >= £16 -> floor £13 guaranteed
  Step 3: floating profit >= £24 -> floor £20 guaranteed
Once a rung locks, the position cannot close below that floor.

INSTRUMENT CONTEXT
US500 via Capital.com, priced in USD per index point (~7,500 in 2026). Stop 30pt
(trailing), target 45pt, spread ~0.6pt. Average daily range 50-80 points. P&L reported
in GBP and USD.

INDICATOR HIERARCHY
TIER 1: daily SSL (trend -- BULL sets LONG direction, BEAR sets SHORT direction), 1h SSL
  (confirmation; a brief counter-trend bar with 1h RSI still supporting the trend is a
  valid dip/rally entry), 1h RSI (LONG: >55 strong, 45-55 a buyable pullback; SHORT:
  <45 strong, 45-55 a sellable rally).
TIER 2: MACD histogram, TMO. TIER 3: Chande MO, Money Flow.
5-MINUTE ENTRY: 5 of 6 indicators agree; last candle GREEN; 5m TMO above +0.3.

TRADING MEMORY (Merlin's Memory) -- LEARN FROM YOURSELF
You are given a TRADING MEMORY section below: your own recent completed trades, recent
STAY-OUT (phantom) decisions judged against a fair benchmark, and patterns observed in
your own trading. USE IT -- avoid repeating past mistakes and reinforce what has worked.
It is context to sharpen your judgement; it does NOT change your confidence floor. When
memory is sparse ("building memory"), rely on the indicators. (Note: ESTrader has no
Morgan gate -- your assessment and the Lancelot pre-checks alone decide entries.)

DISCRIMINATE OVER CAUTION
Your job is to DISCRIMINATE between good and poor setups -- not to default to caution. A clean setup deserves a HIGH confidence score and a trade; a poor setup a LOW score and a stay-out. Both are equally valid. Capital preservation comes from ACCURATE ASSESSMENT, not from systematically avoiding trades.

CONFIDENCE CALIBRATION (your score MUST discriminate):
65-80 = clean 6/6 setup (all SSL aligned + momentum agrees + RSI confirming) -> trade with conviction.
40-60 = most agree, 1-2 mixed -> merit + caution.  20-39 = significantly mixed/conflicting -> stay-out likely correct.  <20 = substantial disagreement -> no trade.
A 35 on a clean 6/6 setup is WRONG; a 35 on a mixed setup is right. Force above 60 when all indicators agree.
US-SPECIFIC: directional moves driven by macro/earnings/US-open flow. Clean setup = all SSL aligned + momentum (TMO/MACD) agree + RSI confirming = 65-75. Mixed signals on a volatile earnings/data day = 30-40.

DECISION HIERARCHY -- HOW TO USE YOUR TOOLS (Guinevere 2.0, Commission 018)
LEVEL 1 -- GUINEVERE 2.0 (highest priority WHEN ACTIVE). The GUINEVERE 2.0 -- MACRO
INTELLIGENCE block (shown with the market data) is current real-world information your
technical indicators cannot yet reflect -- news moves markets BEFORE indicators catch up.
When Guinevere fires a HIGH or MEDIUM signal:
  -> Trust Guinevere's DIRECTION over the daily SSL. The daily SSL reflects where price
     HAS BEEN; Guinevere tells you where price IS GOING.
  -> Reduce reliance on Money Flow and Chande MO -- on news days momentum FOLLOWS the news.
  -> Your CORE ENTRY-TIMING indicators remain fully valid (see Level 2): they tell you WHEN
     to enter; Guinevere tells you WHETHER the macro environment supports the trade.
  -> Apply Guinevere's confidence modifier (capped +/-25) to your own score, in its
     direction. If Guinevere is MIXED/uncertain, treat it as LOW conviction and lean
     STAY_OUT unless Level 2 strongly agrees.
When Guinevere is NEUTRAL: weight all indicators normally; the daily SSL keeps full weight.
LEVEL 2 -- CORE INDICATORS (always valid): 1hr SSL, 5min SSL, RSI, TMO, MACD (and the
  Efficiency Ratio where shown). These are your entry-timing tools regardless of news.
LEVEL 3 -- SUPPORTING (context, not veto): daily SSL (overridden by an ACTIVE Guinevere),
  Money Flow, Chande MO. Do NOT let these talk you out of a strong Guinevere + Level 2 combo.
LEVEL 4 -- MORGAN (performance memory): context only; never overrides Guinevere + Level 2.
GOLDEN RULE: strong Guinevere signal + aligned Level 2 = ENTER with conviction (65-80); do
not let Level 3 or a quiet Morgan block a clear setup. AVOIDING PARALYSIS: Guinevere active
-> follow Guinevere + Level 2; Guinevere neutral -> follow the majority of Level 2; still
conflicted -> confidence 35-40 -> STAY_OUT. A confident wrong trade is worse than a
cautious pass. (Per Principle 14, Guinevere is context + a capped modifier, never a filter.)

HARD RULES -- NEVER VIOLATE
1.  Trade the daily-SSL direction only. Daily BULL -> LONG; daily BEAR -> SHORT; daily
    NEUTRAL/unclear -> STAY_OUT. Never counter-trend the daily.
2.  Calibrate confidence to the session phase (US_SESSION > PRE_MARKET > ASIAN); avoid the first ~15 min after the 13:30 US open (opening volatility). No NYSE-hours-only restriction.
3.  Never enter within 30 min of an FOMC / NFP / CPI / GDP release.
4.  Force close all positions by 20:55 UTC -- never hold overnight.
5.  Act on valid trend-aligned setups scoring 50+ -- do not be overcautious.
6.  30-point trailing stop gives room -- do NOT exit early on noise.

LONG ENTRY -- in a confirmed uptrend (daily SSL BULL), look for:
  Daily SSL = BULL
  1h SSL = BULL, OR 1h SSL = BEAR with 1h RSI > 45 (valid pullback dip)
  5m momentum turning back up (5/6 bullish, GREEN candle, 5m TMO > +0.3)
  Session tradeable (US_SESSION best; lower confidence in PRE_MARKET/ASIAN), calendar clear, confidence >= 50

SHORT ENTRY -- in a confirmed downtrend (daily SSL BEAR), look for:
  Daily SSL = BEAR
  1h SSL = BEAR, OR 1h SSL = BULL with 1h RSI < 55 (valid rally sell)
  5m momentum turning back down (5/6 bearish, RED candle, 5m TMO < -0.3)
  Session tradeable (US_SESSION best; lower confidence in PRE_MARKET/ASIAN), calendar clear, confidence >= 50

EXIT when in position:
  5m SSL reverses AND RSI crosses 50 (both required)
  OR force close signal at 20:55 UTC

REQUIRED OUTPUT -- valid JSON only. No markdown, no preamble.
{
  "decision": "ENTER_LONG | ENTER_SHORT | HOLD | EXIT | STAY_OUT",
  "confidence": 0-100,
  "session_bias": "US_OPEN_VOLATILE | CORE_ACTIVE | LATE_CAUTION | CLOSED",
  "reasoning": "2-4 sentences explaining your decision",
  "warnings": ["list of concerns"],
  "checklist": {
    "trend_aligned": true,
    "momentum_confirmed": true,
    "session_appropriate": true,
    "calendar_clear": true,
    "past_open_volatility": true,
    "high_conviction": true
  },
  "calendar_assessment": "brief comment on upcoming US events",
  "session_assessment": "brief comment on session phase and timing"
}"""


# ── Format indicators for Arthur ──────────────────────────────────────────────

def _regime_block(regime: Optional[dict], min_confidence) -> str:
    """Live BULL/BEAR regime context + confidence-floor block for Arthur.

    Bidirectional (23 Jul 2026): regime is CONTEXT only -- it no longer gates or biases
    direction. The daily SSL sets direction; LONG and SHORT share the same confidence
    floor. This block just tells Arthur which way the market is leaning.
    """
    if not regime:
        return ("REGIME CONTEXT (current)\n"
                "  Regime: unknown | direction set by daily SSL | min confidence to act: 50")
    reg = regime.get("regime", "BULL")
    mc = 50 if min_confidence is None else int(min_confidence)
    lvl = regime.get("sp500_level"); ma = regime.get("sp500_200ma")
    vix = regime.get("vix_level")
    pos = ("ABOVE" if regime.get("sp500_above_200ma") else "AT/BELOW")
    return (
        "REGIME CONTEXT (current)\n"
        f"  Regime: {reg}  (S&P {pos} 200MA"
        + (f": {lvl} vs {ma}" if lvl and ma else "") + f", VIX {vix})\n"
        "  Bidirectional: the daily SSL sets today's direction (BULL -> LONG, "
        "BEAR -> SHORT). Regime is context, not a gate.\n"
        f"  Minimum confidence to open (LONG or SHORT): {mc} (the system enforces this floor)."
    )


def _format_indicators(
    bar_1d: Optional[pd.Series],
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    session_phase: str,
    current_trade=None,
    calendar_context: Optional[str] = None,
    memory_context: Optional[str] = None,
    regime: Optional[dict] = None,
    min_confidence=None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    regime_block = _regime_block(regime, min_confidence)

    def _f(v, dp=2):
        if v is None or pd.isna(v):
            return "N/A"
        return f"{float(v):.{dp}f}"

    candle_colour = "GREEN" if bar_5m.get("close", 0) >= bar_5m.get("open", 0) else "RED"
    ssl_1d = "BULL" if (bar_1d is not None and bar_1d.get("ssl_bull")) else ("BEAR" if bar_1d is not None else "N/A (no daily data)")
    ssl_1h = "BULL" if bar_1h.get("ssl_bull") else "BEAR"
    ssl_5m = "BULL" if bar_5m.get("ssl_bull") else "BEAR"

    position_text = "None -- no open position"
    if current_trade is not None:
        pts_from_entry = (current_price - current_trade.entry_price) if current_trade.direction == "LONG" \
                         else (current_trade.entry_price - current_price)
        position_text = (
            f"OPEN {current_trade.direction} | "
            f"entry={current_trade.entry_price:.1f} | "
            f"current={current_price:.1f} | "
            f"pts_from_entry={pts_from_entry:+.1f} | "
            f"stop={current_trade.stop_loss:.1f} | "
            f"target={current_trade.take_profit:.1f} | "
            f"stake=£{current_trade.stake:.2f}/pt | "
            f"session={current_trade.session_phase}"
        )

    if current_trade is not None and getattr(current_trade, "ladder_step", 0):
        position_text += (
            " | PROFIT LADDER ACTIVE: floor locked at £%.2f (step %d). Position cannot "
            "close below this floor unless a gap event occurs -- factor this into your "
            "HOLD reasoning." % (getattr(current_trade, "ladder_floor_gbp", 0.0),
                                 int(getattr(current_trade, "ladder_step", 0))))

    return f"""Please analyse the current US S&P 500 (US500) market conditions.

TIME AND PRICE
  Time (UTC):       {now}
  Session Phase:    {session_phase}
  US500 Level:      {current_price:,.1f}

{regime_block}

DAILY CHART (Trend Direction -- sets allowed direction for today)
  SSL Cloud:        {ssl_1d}
  RSI (14):         {_f(bar_1d.get('rsi') if bar_1d is not None else None, 1)}
  TMO Main:         {_f(bar_1d.get('tmo_main') if bar_1d is not None else None, 3)}
  Chande MO (20):   {_f(bar_1d.get('chande_mo') if bar_1d is not None else None, 1)}

1-HOUR CHART (Trend Confirmation)
  SSL Cloud:        {ssl_1h}
  RSI (14):         {_f(bar_1h.get('rsi'), 1)}
  MACD Histogram:   {_f(bar_1h.get('macd_histogram'), 3)}
  TMO Main:         {_f(bar_1h.get('tmo_main'), 3)}
  TMO Smooth:       {_f(bar_1h.get('tmo_smooth'), 3)}
  Chande MO (20):   {_f(bar_1h.get('chande_mo'), 1)}
  Money Flow (14):  {_f(bar_1h.get('money_flow'), 2)}

5-MINUTE CHART (Entry Timing)
  SSL Cloud:        {ssl_5m}
  RSI (14):         {_f(bar_5m.get('rsi'), 1)}
  MACD Histogram:   {_f(bar_5m.get('macd_histogram'), 3)}
  TMO Main:         {_f(bar_5m.get('tmo_main'), 3)}
  TMO Smooth:       {_f(bar_5m.get('tmo_smooth'), 3)}
  Chande MO (20):   {_f(bar_5m.get('chande_mo'), 1)}
  Money Flow (14):  {_f(bar_5m.get('money_flow'), 2)}
  Last Candle:      {candle_colour} (close={_f(bar_5m.get('close'), 1)} open={_f(bar_5m.get('open'), 1)})

CURRENT POSITION
  {position_text}

{calendar_context if calendar_context else 'US ECONOMIC CALENDAR\n  No calendar data available.'}

{memory_context if memory_context else 'TRADING MEMORY -- YOUR RECENT DECISIONS ON US500\n  Building memory -- insufficient history yet. Trade on the indicators.'}

Please provide your analysis and trading decision in the required JSON format."""


# ── Main decision function ────────────────────────────────────────────────────

def get_trading_decision(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    session_phase: str,
    bar_1d: Optional[pd.Series] = None,
    current_trade=None,
    calendar_context: Optional[str] = None,
    memory_context: Optional[str] = None,
    regime: Optional[dict] = None,
    min_confidence=None,
    guinevere_advisory: Optional[str] = None,
) -> dict:
    """
    Send indicator data to Arthur (Claude) and receive a trading decision.
    Only call this AFTER Lancelot pre-checks have passed.
    """
    log.info("Sending indicators to Arthur...")

    user_message = _format_indicators(
        bar_1d, bar_1h, bar_5m, current_price, session_phase,
        current_trade, calendar_context, memory_context, regime, min_confidence,
    )
    # Guinevere 2.0 advisory (Commission 018): prepend so Arthur reads the macro context
    # ABOVE the market data. Merlin's Memory stays where it is (inside the indicators block).
    if guinevere_advisory:
        user_message = guinevere_advisory + "\n\n" + user_message

    for attempt in range(2):
        try:
            response = client.messages.create(
                model      = "claude-sonnet-4-6",
                max_tokens = 2000,
                system     = [{"type": "text", "text": SYSTEM_PROMPT,
                               "cache_control": {"type": "ephemeral"}}],
                messages   = [{"role": "user", "content": user_message}],
            )

            if response.stop_reason == "max_tokens":
                log.warning("Arthur hit max_tokens -- JSON may be truncated")

            raw_text = response.content[0].text.strip()
            if raw_text.startswith("```"):
                raw_text = "\n".join(
                    l for l in raw_text.split("\n")
                    if not l.strip().startswith("```")
                ).strip()

            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                log.error("Arthur returned invalid JSON (attempt %d/2): %s", attempt + 1, exc)
                if attempt == 0:
                    continue
                return _safe_stay_out("Arthur returned invalid JSON -- staying out for safety")

            decision["timestamp"]     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            decision["tokens_used"]   = response.usage.input_tokens + response.usage.output_tokens
            decision["current_price"] = current_price
            decision["session_phase"] = session_phase

            log.info(
                "Arthur decision: %s | confidence=%s | tokens=%d",
                decision.get("decision"),
                decision.get("confidence"),
                decision.get("tokens_used", 0),
            )
            return decision

        except anthropic.APIError as exc:
            log.error("Anthropic API error: %s", exc)
            return _safe_stay_out(f"API error: {str(exc)}")
        except Exception as exc:
            log.error("Unexpected error calling Arthur: %s", exc)
            return _safe_stay_out(f"Unexpected error: {str(exc)}")

    return _safe_stay_out("Arthur failed after all attempts")


def _safe_stay_out(reason: str) -> dict:
    return {
        "decision":            "STAY_OUT",
        "confidence":          0,
        "session_bias":        "CLOSED",
        "reasoning":           reason,
        "warnings":            [reason],
        "checklist":           {},
        "calendar_assessment": "",
        "session_assessment":  "",
        "timestamp":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tokens_used":         0,
    }


def format_decision_for_display(decision: dict) -> str:
    """Format Arthur's decision for terminal display."""
    d         = decision.get("decision", "UNKNOWN")
    conf      = decision.get("confidence", "--")
    bias      = decision.get("session_bias", "--")
    reasoning = decision.get("reasoning", "No reasoning")
    warnings  = decision.get("warnings", [])
    tokens    = decision.get("tokens_used", 0)
    ts        = decision.get("timestamp", "")
    price     = decision.get("current_price", "--")
    price_str = f"{price:,.1f}" if isinstance(price, (int, float)) else str(price)
    lines = [
        "=" * 60,
        "  ESTrader AI -- Arthur's Decision",
        f"  {ts}",
        "=" * 60,
        f"  Decision:        {d}",
        f"  Confidence:      {conf}/100",
        f"  Session Bias:    {bias}",
        f"  US500 Level:     {price_str}",
        f"  Session Phase:   {decision.get('session_phase', '--')}",
        "",
        "  Reasoning:",
        f"  {reasoning}",
        "",
    ]
    if warnings:
        lines.append("  Warnings:")
        for w in warnings:
            lines.append(f"    - {w}")
        lines.append("")
    cal = decision.get("calendar_assessment")
    if cal:
        lines.append(f"  Calendar: {cal}")
    ses = decision.get("session_assessment")
    if ses:
        lines.append(f"  Session:  {ses}")
    cl = decision.get("checklist", {})
    if cl:
        lines.append("  Checklist:")
        for k, v in cl.items():
            icon = "PASS" if v else "FAIL"
            lines.append(f"    [{icon}] {k.replace('_', ' ').title()}")
    lines.append(f"  Tokens used: {tokens}")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Arthur self-test -- calling Claude with a bullish US500 setup...")
    bar_1d = pd.Series({"ssl_bull": True, "rsi": 58.0, "tmo_main": 1.5, "chande_mo": 25.0})
    bar_1h = pd.Series({
        "ssl_bull": True, "rsi": 62.0, "macd_histogram": 8.5,
        "tmo_main": 2.1, "tmo_smooth": 1.5, "chande_mo": 45.0, "money_flow": 150.0,
    })
    bar_5m = pd.Series({
        "ssl_bull": True, "rsi": 58.0, "macd_histogram": 2.5,
        "tmo_main": 0.8, "tmo_smooth": 0.5, "chande_mo": 30.0, "money_flow": 80.0,
        "open": 7490.0, "close": 7500.0,
    })
    decision = get_trading_decision(
        bar_1h=bar_1h, bar_5m=bar_5m,
        current_price=7500.0, session_phase="US_SESSION", bar_1d=bar_1d,
    )
    print(format_decision_for_display(decision))
    log.info("Arthur self-test complete.")
