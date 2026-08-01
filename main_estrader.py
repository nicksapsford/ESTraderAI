"""
ESTrader AI -- main_estrader.py
US S&P 500 (US500) spread betting main loop.
Mon-Fri, US cash session 14:30-21:00 UTC only.
No entries 14:30-14:45 UTC (open volatility) or after 20:45 UTC.
Force close at 20:45 UTC. No overnight positions, ever.

PAPER_TRADING_MODE = True until demo account is verified.
"""

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

PAPER_TRADING_MODE = True
VERSION            = ((Path(__file__).resolve().parent / "VERSION").read_text().strip()
                      if (Path(__file__).resolve().parent / "VERSION").exists() else "1.5.2")
CANDLE_INTERVAL    = 300      # 5-minute candle loop (seconds)
POSITION_INTERVAL  = 30       # position monitoring (seconds)
HEARTBEAT_INTERVAL = 240      # emit a liveness log at least this often, even when idle
DASHBOARD_INTERVAL = 15       # push live top-line state to the dashboard this often
BASE_DIR           = Path(__file__).resolve().parent
LOG_DIR            = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG      = LOG_DIR / "shutdown.flag"

# Session boundaries (minute-of-day, UTC). ESTrader trades ~23h; US_SESSION is 13:30-21:00.
_PRE_MARKET_START = 13 * 60 + 30   # 13:30 (US cash open)
_MARKET_OPEN      = 14 * 60 + 30   # 14:30
_FORCE_CLOSE      = 20 * 60 + 55   # 20:55 -- force close before the daily maintenance break
_SESSION_END      = 21 * 60        # 21:00 -- US cash close / maintenance begins

# ── Env / logging setup ───────────────────────────────────────────────────────

_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    _TIDE_ENV = BASE_DIR.parent / "TideTraderAI" / ".env"
    if _TIDE_ENV.exists():
        load_dotenv(dotenv_path=_TIDE_ENV)
    else:
        load_dotenv()

# ─── ALBION STANDING RULE: ALL LOG TIMESTAMPS ARE UTC ────────────────────────
# Force Python's logging to emit %(asctime)s in UTC, not BST/local. Without this
# line, logging defaults to local time and every log line is +1h vs the UTC CSV
# artefacts (es_phantom_trades.csv etc.) — the exact BST/UTC mismatch that caused a
# misread on 11 Jul 2026. Never interpret an Albion log timestamp as local time;
# confirm UTC before analysing. (Baked in per Nick's directive, 12 Jul 2026.)
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "estrader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ESTrader.Main")

# ── Internal imports ──────────────────────────────────────────────────────────

from agent_brain_es     import get_trading_decision, format_decision_for_display
from calendar_es        import check_calendar, is_hard_blocked, get_calendar_context
from data_feed_es       import (
    USDataFeed, get_session_phase, is_market_open, minutes_until_next_open,
)
from capitalcom_connector import CapitalComConnector
from notifier_es        import (
    notify_system_startup, notify_system_shutdown,
    notify_trade_opened, notify_trade_closed_win, notify_trade_closed_loss,
    notify_kill_switch_triggered, notify_kill_switch_reset,
    notify_calendar_block, notify_daily_summary, notify_system_error,
    notify_memory_pattern,
)
from paper_trader_es    import PaperTraderES, TRADES_LOG
from pre_checks_es      import run_all_pre_checks, run_individual_pre_checks
from strategy_es        import should_force_close, DEFAULT_GBPUSD
import phantom_tracker
import benchmark_link
import merlin_memory_es   # Merlin's Memory -- Arthur's episodic recall (ESTrader pilot)
try:
    import guinevere2                       # Guinevere 2.0 directional news (Commission 018)
except Exception:
    guinevere2 = None
import regime_es   # market-regime reader (context only; NOT a gate)
# NOTE (ESTrader, 27 Jul 2026): Morgan (performance_es) is REMOVED by design -- this
# pilot tests Merlin's Memory alone. Only Lancelot pre-checks and the daily-loss kill
# switch can block Arthur; there is no Morgan confidence gate / hard block.

US_EPIC     = "US500"
GBPUSD_EPIC = "GBPUSD"

# Minimum Arthur confidence to open a LONG (System 4 Review, Change 1). Regime-aware:
# a confirmed bull market (S&P above 200MA) uses a lower bar; a bear market reverts to
# the stricter bar. NOTE: no such gate existed before -- this CREATES it (see report).
ARTHUR_MIN_CONFIDENCE_BULL = 50
ARTHUR_MIN_CONFIDENCE_BEAR = 50   # bidirectional: no bear asymmetry (unused; kept =BULL)

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_SHUTDOWN = False

def _handle_signal(sig, frame):
    global _SHUTDOWN
    log.info("Shutdown signal received (%s)", sig)
    _SHUTDOWN = True

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Account state ─────────────────────────────────────────────────────────────


def _today_realised_pnl(csv_path) -> float:
    """Sum today's (UTC) realised P&L from the trade-log CSV (Today P&L Persist Fix,
    30 Jul 2026). Lets a mid-day restart keep the 'today' counter accurate instead of
    resetting to zero. Only CLOSED trades are in this CSV, so open positions are excluded.
    Robust: missing file / no trades today / bad rows -> 0.0. All timestamps are UTC."""
    import csv as _csv
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path
    try:
        p = _Path(csv_path)
        if not p.exists():
            return 0.0
        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        total = 0.0
        with p.open(newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                d = (row.get("date") or "").strip()
                if not d:
                    d = (row.get("entry_time") or "").strip()[:10]
                if d != today:
                    continue
                try:
                    total += float(row.get("pnl_gbp") or 0.0)
                except (TypeError, ValueError):
                    continue
        return round(total, 2)
    except Exception:
        return 0.0


class AccountState:
    """Holds live trading account state passed to pre-checks."""

    def __init__(self, capital: float) -> None:
        self.capital_gbp        = capital
        self.daily_pnl_gbp      = 0.0
        self.consecutive_losses = 0
        self.last_loss_time     = None
        self.kill_switch_active = False
        self.kill_switch_tier   = 0
        self.kill_switch_until  = None
        self.kill_switch_reason = ""
        self.kill_history       = []

    def record_trade(self, pnl_gbp: float) -> None:
        self.daily_pnl_gbp += pnl_gbp
        self.capital_gbp = round(self.capital_gbp + pnl_gbp, 2)
        if pnl_gbp < 0:
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now(timezone.utc)
        else:
            self.consecutive_losses = 0

    def reset_daily(self) -> None:
        self.daily_pnl_gbp = 0.0


# ── GBP/USD rate ──────────────────────────────────────────────────────────────

_gbpusd_cache = {"rate": DEFAULT_GBPUSD, "at": 0.0}


def _get_gbpusd(ig: CapitalComConnector) -> float:
    """Live GBP/USD from Capital.com, cached 60s, with a safe fallback."""
    now = time.monotonic()
    if now - _gbpusd_cache["at"] < 60:
        return _gbpusd_cache["rate"]
    try:
        if ig is not None and ig.connected:
            data = ig.get_price(GBPUSD_EPIC)
            if data:
                # Excalibur rounds mid to 1dp (fine for the index, too coarse for
                # FX) -- recompute from bid/ask to keep GBPUSD precision.
                bid, ask = data.get("bid"), data.get("ask")
                if bid and ask:
                    rate = (float(bid) + float(ask)) / 2
                else:
                    rate = float(data.get("mid") or 0)
                if 0.5 < rate < 3.0:   # sanity band
                    _gbpusd_cache.update(rate=rate, at=now)
                    return rate
    except Exception:
        pass
    _gbpusd_cache["at"] = now
    return _gbpusd_cache["rate"]


# ── Dashboard push (best-effort) ──────────────────────────────────────────────

DASHBOARD_URL = "http://localhost:5007/api/update"

_dash_first_ok:  bool  = False
_dash_fail_count: int  = 0
_dash_last_warn: float = 0.0


def _dashboard_push_ok(kind: str, phase: str, price: float, status: str, http) -> None:
    global _dash_first_ok
    if not _dash_first_ok:
        _dash_first_ok = True
        log.info("Dashboard connected -- first %s push OK | phase=%s us500=%.1f status=%s HTTP %s",
                 kind, phase, price, status, http)
    else:
        log.debug("Dashboard %s push | phase=%s us500=%.1f status=%s HTTP %s",
                  kind, phase, price, status, http)


def _dashboard_push_warn(exc: Exception) -> None:
    global _dash_fail_count, _dash_last_warn
    _dash_fail_count += 1
    now = time.monotonic()
    if now - _dash_last_warn > 60:
        log.warning("Dashboard push failing (%d so far): %s -- is dashboard_es.py running on :5007?",
                    _dash_fail_count, exc)
        _dash_last_warn = now


def _serialise_trade(trade):
    if trade is None:
        return None
    if hasattr(trade, "__dict__"):
        return {k: str(v) for k, v in trade.__dict__.items()}
    return trade


def _safe_float(v):
    try:
        f = float(v)
        return None if f != f else f  # NaN check (NaN != NaN)
    except (TypeError, ValueError):
        return None


def _indicator_snapshot(bar) -> dict:
    if bar is None:
        return {}
    return {
        "ssl_bull":   bool(bar.get("ssl_bull", False)),
        "rsi":        _safe_float(bar.get("rsi")),
        "macd":       _safe_float(bar.get("macd")),
        "tmo_main":   _safe_float(bar.get("tmo_main")),
        "chande_mo":  _safe_float(bar.get("chande_mo")),
        "money_flow": _safe_float(bar.get("money_flow")),
    }


def _ssl_label(ind: dict) -> str:
    """BULL / BEAR / -- from an indicator snapshot's ssl_bull flag."""
    if not ind or "ssl_bull" not in ind:
        return "--"
    return "BULL" if ind["ssl_bull"] else "BEAR"


def _fmt_ind(v) -> str:
    """2dp string for a numeric indicator, or '' if unavailable."""
    return "" if v is None else f"{float(v):.2f}"


def _build_exit_meta(ind_1d: dict, ind_1h: dict, ind_5m: dict, decision: dict) -> dict:
    """Indicator snapshot + Arthur's exit-decision confidence for an ARTHUR_EXIT
    (Gaius Commission 012). Scalars are taken from the 1h snapshot (the confirmation
    timeframe). Best-effort -- returns None on any error so a logging failure never
    blocks a trade close."""
    try:
        ind_1h = ind_1h or {}
        conf = decision.get("confidence") if decision else None
        return {
            "exit_daily_ssl":  _ssl_label(ind_1d),
            "exit_1h_ssl":     _ssl_label(ind_1h),
            "exit_5m_ssl":     _ssl_label(ind_5m),
            "exit_tmo":        _fmt_ind(ind_1h.get("tmo_main")),
            "exit_money_flow": _fmt_ind(ind_1h.get("money_flow")),
            "exit_rsi":        _fmt_ind(ind_1h.get("rsi")),
            "exit_chande_mo":  _fmt_ind(ind_1h.get("chande_mo")),
            "exit_confidence": "" if conf is None else str(conf),
        }
    except Exception:
        return None


def _push_dashboard(
    stanley:    PaperTraderES,
    account:    AccountState,
    decision:   dict = None,
    pre_checks: dict = None,
    phase:      str  = "",
    us_level:   float = 0.0,
    gbpusd:     float = DEFAULT_GBPUSD,
    calendar_summary: str = "",
    connector_status: str = "yahoo",
    panel_mode: str = "pre_checks",
    trend_1d:   str = "NEUTRAL",
    trend_1h:   str = "NEUTRAL",
    signal_5m:  str = "NEUTRAL",
    indicators_1d: dict = None,
    indicators_1h: dict = None,
    indicators_5m: dict = None,
) -> None:
    """Push latest state to dashboard via HTTP POST (separate process)."""
    try:
        import requests
        payload = {
            "mode":          "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "version":       VERSION,
            "epic":          US_EPIC,
            "phase":         phase,
            "us_level":      us_level,
            "gbpusd_rate":   gbpusd,
            "connector_status": connector_status,
            "capital":       stanley.capital_gbp,
            "daily_pnl":     account.daily_pnl_gbp,
            "total_trades":  stanley.total_trades,
            "win_rate":      stanley.win_rate,
            "in_trade":      stanley.in_trade,
            "current_trade": _serialise_trade(stanley.current_trade),
            "decision":      decision,
            "panel_mode":    panel_mode,
            "checklist":     (decision or {}).get("checklist", {}),
            "pre_checks":    pre_checks,
            "trend_1d":      trend_1d,
            "trend_1h":      trend_1h,
            "signal_5m":     signal_5m,
            "indicators_1d": indicators_1d or {},
            "indicators_1h": indicators_1h or {},
            "indicators_5m": indicators_5m or {},
            "calendar":      calendar_summary,
            "memory":        merlin_memory_es.get_memory_stats(),
            "kill_switch":   account.kill_switch_active,
            "kill_tier":     account.kill_switch_tier,
            "updated_at":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        resp = requests.post(
            DASHBOARD_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=2,
        )
        _dashboard_push_ok("full", phase, us_level, connector_status, resp.status_code)
    except Exception as exc:
        _dashboard_push_warn(exc)


def _push_dashboard_live(
    stanley: PaperTraderES,
    account: AccountState,
    ig:      CapitalComConnector,
    feed:    USDataFeed,
    now_utc: datetime,
) -> None:
    """
    Lightweight, frequent push of the always-known top-line fields (live price,
    phase, connector status, capital, P&L, open position). Runs every loop tick
    in ALL session phases so the dashboard never sits on its 0.0 / -- defaults.
    """
    try:
        import requests
        phase = get_session_phase(now_utc)
        price = _get_price(ig, feed)
        gbpusd = _get_gbpusd(ig)
        connector_status = "capitalcom" if (ig is not None and ig.connected) else "yahoo"
        payload = {
            "mode":             "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "version":          VERSION,
            "epic":             US_EPIC,
            "phase":            phase,
            "us_level":         price,
            "gbpusd_rate":      gbpusd,
            "connector_status": connector_status,
            "capital":          stanley.capital_gbp,
            "daily_pnl":        account.daily_pnl_gbp,
            "total_trades":     stanley.total_trades,
            "win_rate":         stanley.win_rate,
            "in_trade":         stanley.in_trade,
            "current_trade":    _serialise_trade(stanley.current_trade),
            "kill_switch":      account.kill_switch_active,
            "kill_tier":        account.kill_switch_tier,
            "memory":           merlin_memory_es.get_memory_stats(),
            "updated_at":       now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        resp = requests.post(
            DASHBOARD_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=2,
        )
        _dashboard_push_ok("live", phase, price, connector_status, resp.status_code)
    except Exception as exc:
        _dashboard_push_warn(exc)


# ── Core candle tick ──────────────────────────────────────────────────────────

def run_candle_tick(
    feed:    USDataFeed,
    stanley: PaperTraderES,
    account: AccountState,
    ig:      CapitalComConnector,
) -> None:
    """
    Called once every 5 minutes during a trading session.
    Gathers indicators, runs pre-checks, calls Arthur, acts on decision.
    """
    now_utc    = datetime.now(timezone.utc)
    phase      = get_session_phase(now_utc)
    us_price   = _get_price(ig, feed)
    gbpusd     = _get_gbpusd(ig)
    connector_status = "capitalcom" if (ig is not None and ig.connected) else "yahoo"

    log.info("--- CANDLE TICK | %s | phase=%s | US500=%.1f | GBPUSD=%.4f ---",
             now_utc.strftime("%H:%M:%S UTC"), phase, us_price, gbpusd)

    # Gaius Commission 012: fill any ARTHUR_EXIT row's post-exit prices once 30/60 min
    # have elapsed (runs every tick, survives restarts, never raises).
    stanley.fill_post_exit_prices(us_price, now_utc)

    # Calendar check
    hard_blocked, block_reason, event_name, mins_remain = is_hard_blocked(now_utc)
    cal_context = get_calendar_context(now_utc)
    cal_summary = check_calendar(now_utc).get("calendar_summary", "")

    if hard_blocked:
        log.warning("CALENDAR HARD BLOCK: %s (%d min remaining)", block_reason, mins_remain)
        notify_calendar_block(event_name or block_reason, mins_remain)
        if not stanley.in_trade:
            _push_dashboard(stanley, account, phase=phase, us_level=us_price, gbpusd=gbpusd,
                            calendar_summary=cal_summary, connector_status=connector_status)
            return

    # Refresh data
    try:
        feed.refresh()
    except Exception as exc:
        log.error("Data refresh failed: %s", exc)
        return

    bar_1d = feed.latest_bar("1d")
    bar_1h = feed.latest_bar("1h")
    bar_5m = feed.latest_bar("5m")

    if bar_1h is None or bar_5m is None:
        log.warning("Insufficient indicator data -- skipping tick")
        return

    # Merlin's Memory (ESTrader pilot) REPLACES Morgan's performance context: Arthur
    # receives episodic recall of his own past trades / phantom stay-outs / observed
    # patterns each consult, regenerated fresh from live CSV data.
    try:
        memory_context = merlin_memory_es.get_memory_summary()
    except Exception as _mexc:
        log.warning("Merlin's Memory build failed: %s", _mexc)
        memory_context = ""

    sig_1h = feed.composite_signal("1h")
    sig_5m = feed.composite_signal("5m")
    trend_1d = "LONG" if bar_1d.get("ssl_bull") else "SHORT"

    # BIDIRECTIONAL (Nick's direct order, 23 Jul 2026 -- removed the LONG_ONLY restriction
    # from the System 4 Review). The daily SSL sets the session direction: BULL -> LONG,
    # BEAR -> SHORT. Lancelot's symmetric daily-trend + SSL-agreement checks govern entry;
    # SHORTs take the SAME pre-checks, sizing and confidence bar as LONGs (no direction bias).
    _ssl_1d = bar_1d.get("ssl_bull") if bar_1d is not None else None
    proposed_direction = "NEUTRAL" if (_ssl_1d is None or _ssl_1d != _ssl_1d) else trend_1d

    # Regime is CONTEXT for Arthur only now -- no longer a direction or confidence gate
    # (bidirectional order, 23 Jul 2026). Same confidence bar for LONG and SHORT.
    regime = regime_es.get_regime()
    min_conf = ARTHUR_MIN_CONFIDENCE_BULL

    ind_1d = _indicator_snapshot(bar_1d)
    ind_1h = _indicator_snapshot(bar_1h)
    ind_5m = _indicator_snapshot(bar_5m)

    checks = run_all_pre_checks(
        bar_1h=bar_1h, bar_5m=bar_5m, account=account,
        current_trade=stanley.current_trade, bar_1d=bar_1d,
        proposed_direction=proposed_direction,
    )
    individual_checks = run_individual_pre_checks(
        bar_1h=bar_1h, bar_5m=bar_5m, account=account,
        current_trade=stanley.current_trade, bar_1d=bar_1d,
        proposed_direction=proposed_direction,
    )

    # GUINEVERE 2.0 HIGH-ALERT active consultation (31 Jul 2026): a HIGH-confidence Guinevere
    # signal may force an Arthur consult even when ONLY soft Lancelot checks block. The
    # re-run relaxes ONLY the soft quality checks (SSL alignment / momentum / candle);
    # safety checks + daily-trend + RSI + choppy are always enforced, so no risk control is
    # bypassed. Arthur still needs >= 60 confidence to act (enforced after the decision).
    guin_high_alert = False
    _guin_alert_note = None
    if (not checks["passed"]) and guinevere2 is not None and stanley.current_trade is None:
        try:
            _al, _favdir, _note = guinevere2.is_high_alert("US500")
            if _al:
                _relaxed = run_all_pre_checks(
                    bar_1h=bar_1h, bar_5m=bar_5m, account=account,
                    current_trade=stanley.current_trade, bar_1d=bar_1d,
                    proposed_direction=_favdir, relax_soft=True)
                if _relaxed["passed"]:
                    checks = _relaxed
                    guin_high_alert = True
                    _guin_alert_note = _note
                    proposed_direction = _favdir
                    log.warning("GUINEVERE HIGH ALERT: soft checks relaxed (%s). %s", _favdir, _note)
        except Exception as _e:
            log.warning("Guinevere high-alert relaxation failed: %s", _e)

    if not checks["passed"]:
        log.info("Pre-checks FAILED: %s", checks.get("reason"))
        _push_dashboard(stanley, account, pre_checks=individual_checks,
                        phase=phase, us_level=us_price, gbpusd=gbpusd, calendar_summary=cal_summary,
                        connector_status=connector_status, panel_mode="pre_checks",
                        trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                        indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)

        if checks.get("kill_switch_triggered"):
            account.kill_switch_active = True
            tier = checks.get("kill_tier", 1)
            account.kill_switch_tier   = tier
            wait_hours = {1: 6, 2: 12}.get(tier, 24)
            account.kill_switch_until  = None
            notify_kill_switch_triggered(
                tier=tier, reason=checks.get("reason", ""),
                wait_hours=wait_hours,
                daily_pnl=account.daily_pnl_gbp,
                capital=stanley.capital_gbp,
            )
        elif checks.get("kill_switch_reset"):
            account.kill_switch_active = False
            notify_kill_switch_reset(
                tier=account.kill_switch_tier, wait_hours=0,
                capital=stanley.capital_gbp,
            )
            account.kill_switch_tier = 0
        return

    # NOTE (ESTrader): no Morgan hard block here -- Morgan is removed by design. Arthur
    # is gated only by Lancelot pre-checks (above) and the daily-loss kill switch.

    # Guinevere 2.0 -- directional macro intelligence for Arthur (Commission 018). Advises
    # Arthur IN THE PROMPT (DECISION HIERARCHY) above the market data; Merlin's Memory stays
    # inside the indicators block. Fail-safe: any failure yields a NEUTRAL advisory and never
    # blocks the consult.
    guin_advisory, guin_sig = None, None
    if guinevere2 is not None:
        try:
            guin_sig = guinevere2.get_signal("US500")
            guin_advisory = guinevere2.get_advisory("US500")
        except Exception as _e:
            log.warning("Guinevere 2.0 failed: %s", _e)
    # On a HIGH-alert relaxed consult, tell Arthur it is a Guinevere-forced consultation
    # (soft checks relaxed, direction supplied, 60+ required).
    if guin_high_alert and _guin_alert_note and guin_advisory:
        guin_advisory = _guin_alert_note + "\n\n" + guin_advisory

    # Call Arthur
    decision = get_trading_decision(
        bar_1h=bar_1h, bar_5m=bar_5m, current_price=us_price,
        session_phase=phase, bar_1d=bar_1d,
        current_trade=stanley.current_trade,
        calendar_context=cal_context, memory_context=memory_context,
        regime=regime, min_confidence=min_conf,
        guinevere_advisory=guin_advisory,
    )

    # Guinevere 2.0 signal logging for ongoing Gaius assessment (Commission 018): log the
    # signal + Arthur's response so Gaius can score Guinevere 2.0's value over time.
    if guinevere2 is not None and guin_sig is not None:
        try:
            guinevere2.log_decision(guin_sig,
                                    arthur_decision=decision.get("decision", ""),
                                    arthur_confidence_after=decision.get("confidence", ""))
        except Exception as _e:
            log.warning("Guinevere 2.0 logging failed: %s", _e)

    # Merlin's Memory: log this consultation (Gaius can later assess whether memory
    # improves decision quality) and fire Percival once on any brand-new n>=3 pattern.
    try:
        merlin_memory_es.log_consultation(decision.get("confidence"))
        for _pk in merlin_memory_es.pop_new_patterns():
            notify_memory_pattern(_pk)
    except Exception as _mexc:
        log.warning("memory logging failed: %s", _mexc)

    # GUINEVERE HIGH-ALERT floor: on a relaxed (soft-check-bypassed) consult, require Arthur
    # >= 60 confidence to enter -- a higher bar precisely because soft confirmation was relaxed.
    if guin_high_alert and decision.get("decision") in ("ENTER_LONG", "ENTER_SHORT"):
        try:
            if float(decision.get("confidence") or 0) < 60:
                log.warning("GUINEVERE HIGH ALERT: Arthur confidence %.0f < 60 on relaxed "
                            "consult -- STAY_OUT.", float(decision.get("confidence") or 0))
                decision["decision"] = "STAY_OUT"
                decision["guinevere_high_alert_floor"] = True
        except (TypeError, ValueError):
            decision["decision"] = "STAY_OUT"

    log.info(format_decision_for_display(decision))
    _push_dashboard(stanley, account, decision=decision, pre_checks=individual_checks,
                    phase=phase, us_level=us_price, gbpusd=gbpusd, calendar_summary=cal_summary,
                    connector_status=connector_status, panel_mode="arthur",
                    trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                    indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)

    action = decision.get("decision", "STAY_OUT")
    _conf = decision.get("confidence")
    try:
        _conf = float(_conf) if _conf is not None else 0.0
    except (TypeError, ValueError):
        _conf = 0.0

    if action == "ENTER_LONG" and not stanley.in_trade:
        # Bidirectional (23 Jul 2026): LONG and SHORT share one confidence bar
        # (ARTHUR_MIN_CONFIDENCE_BULL); regime no longer raises/lowers it.
        if _conf < min_conf:
            log.info("LONG blocked -- confidence %.0f below threshold %d (%s regime)",
                     _conf, min_conf, regime.get("regime"))
            action = "STAY_OUT"
        else:
            _open_trade(stanley, account, ig, "LONG", us_price, phase, gbpusd)
    elif action == "ENTER_SHORT" and not stanley.in_trade:
        # Bidirectional (23 Jul 2026): SHORTs take the SAME confidence bar as LONGs.
        if _conf < min_conf:
            log.info("SHORT blocked -- confidence %.0f below threshold %d", _conf, min_conf)
            action = "STAY_OUT"
        else:
            _open_trade(stanley, account, ig, "SHORT", us_price, phase, gbpusd)
    elif action == "EXIT" and stanley.in_trade:
        # Gaius Commission 012: capture the indicator snapshot + Arthur's exit confidence
        # so we can later judge whether the early exit was skill or premature.
        _emeta = _build_exit_meta(ind_1d, ind_1h, ind_5m, decision)
        _close_trade(stanley, account, ig, us_price, "ARTHUR_EXIT", gbpusd, exit_meta=_emeta)
    elif action == "HOLD" and stanley.in_trade:
        log.info("Arthur says HOLD -- maintaining position")
    elif action == "STAY_OUT":
        log.info("Arthur says STAY_OUT -- no action")
        try:
            _dir = proposed_direction if proposed_direction in ("LONG", "SHORT") else ("LONG" if bar_1d.get("ssl_bull") else "SHORT")
            try:
                _snap = phantom_tracker.build_snapshot(
                    ind_1d, ind_1h, ind_5m,
                    morgan_score=None,   # Morgan removed on ESTrader -- column stays blank
                    session=phase,
                )
            except Exception as _se:
                log.warning("phantom indicator snapshot failed: %s", _se)
                _snap = None
            try:
                _bstate, _bavail = benchmark_link.read_availability()
            except Exception:
                _bstate, _bavail = ("UNKNOWN", None)
            phantom_tracker.record_decision(
                market="US500",
                direction_blocked=_dir,
                price_at_decision=us_price,
                confidence=decision.get("confidence"),
                reason_for_stay_out="ARTHUR_STAY_OUT",
                get_price_fn=lambda m: _get_price(ig, feed),
                indicators=_snap,
                benchmark_state=_bstate,
                benchmark_available=_bavail,
            )
        except Exception as _exc:
            log.warning("phantom_tracker record failed: %s", _exc)



# ── Position monitoring ───────────────────────────────────────────────────────

def monitor_open_position(
    stanley:  PaperTraderES,
    account:  AccountState,
    ig:       CapitalComConnector,
    feed:     USDataFeed,
) -> None:
    """Called every 30 seconds while in a position. Trailing stop + force close."""
    if not stanley.in_trade:
        return

    now_utc  = datetime.now(timezone.utc)
    us_price = _get_price(ig, feed)
    gbpusd   = _get_gbpusd(ig)

    if should_force_close(now_utc):
        log.warning("Force close at 20:55 UTC -- closing before the daily maintenance break")
        _close_trade(stanley, account, ig, us_price, "FORCE_CLOSE_2055", gbpusd)
        return

    reason = stanley.monitor_trade(us_price, gbpusd)
    if reason:
        trade = stanley.trade_history[-1] if stanley.trade_history else None
        _handle_closed_trade(account, trade)
        log.info("Position auto-closed: %s | price=%.1f", reason, us_price)


# ── Open / close helpers ──────────────────────────────────────────────────────

def _open_trade(stanley, account, ig, direction, price, phase, gbpusd):
    trade = stanley.open_trade(direction, price, phase, gbpusd_rate=gbpusd)
    if PAPER_TRADING_MODE:
        log.info("[PAPER] OPEN %s | entry=%.1f | stop=%.1f | target=%.1f | stake=£%.2f/pt",
                 direction, price, trade.stop_loss, trade.take_profit, trade.stake)
    else:
        try:
            ig.open_position(
                epic=US_EPIC, direction="BUY" if direction == "LONG" else "SELL",
                size=trade.stake, stop_distance=trade.stop_pts,
            )
            log.info("[LIVE] OPEN %s via Capital.com | entry=%.1f", direction, price)
        except Exception as exc:
            log.error("Capital.com open_position failed: %s -- position tracked paper only", exc)
            notify_system_error(f"Capital.com open failed: {exc}")

    notify_trade_opened(
        direction=direction, entry_price=price,
        stop_loss=trade.stop_loss, take_profit=trade.take_profit,
        stake=trade.stake, session_phase=phase,
    )
    log.info("Trade opened: %s", trade.summary())


def _close_trade(stanley, account, ig, price, reason, gbpusd, exit_meta=None):
    trade = stanley.close_trade(price, reason, gbpusd, exit_meta=exit_meta)
    if trade is None:
        return
    _handle_closed_trade(account, trade)

    if not PAPER_TRADING_MODE:
        try:
            positions = ig.get_open_positions()
            for pos in positions:
                ig.close_position(
                    deal_id=pos.get("dealId"),
                    direction="SELL" if trade.direction == "LONG" else "BUY",
                    size=trade.stake,
                )
            log.info("[LIVE] Position closed via Capital.com | reason=%s", reason)
        except Exception as exc:
            log.error("Capital.com close_position failed: %s", exc)
            notify_system_error(f"Capital.com close failed: {exc}")

    if trade.pnl_gbp >= 0:
        notify_trade_closed_win(
            direction=trade.direction, exit_price=price,
            pnl_pts=trade.pnl_pts, pnl_gbp=trade.pnl_gbp,
            capital=account.capital_gbp, reason=reason,
        )
    else:
        notify_trade_closed_loss(
            direction=trade.direction, exit_price=price,
            pnl_pts=trade.pnl_pts, pnl_gbp=trade.pnl_gbp,
            capital=account.capital_gbp, reason=reason,
        )


def _handle_closed_trade(account: AccountState, trade) -> None:
    if trade is None:
        return
    account.record_trade(trade.pnl_gbp)
    log.info("Trade result: %s%+.2f GBP | capital=£%.2f",
             "+" if trade.pnl_gbp >= 0 else "", trade.pnl_gbp, account.capital_gbp)


# ── Price getter ──────────────────────────────────────────────────────────────

def _get_price(ig: CapitalComConnector, feed: USDataFeed) -> float:
    """Get current US500 price -- Capital.com first, yfinance fallback."""
    try:
        if ig is not None and ig.connected:
            price_data = ig.get_price(US_EPIC)
            if price_data:
                return price_data.get("mid", 0.0)
    except Exception:
        pass
    try:
        df = feed.get("5m")
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return 0.0


# ── Daily summary ─────────────────────────────────────────────────────────────

_last_summary_date: str = ""


def _maybe_send_daily_summary(stanley: PaperTraderES, account: AccountState) -> None:
    global _last_summary_date
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    if today == _last_summary_date:
        return
    t = now_utc.hour * 60 + now_utc.minute
    if t >= _SESSION_END:   # 21:00 UTC US close
        notify_daily_summary(
            date_str=today, trades=stanley.total_trades,
            pnl_gbp=account.daily_pnl_gbp, capital=stanley.capital_gbp,
            win_rate=stanley.win_rate,
        )
        account.reset_daily()
        _last_summary_date = today
        log.info("Daily summary sent for %s", today)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    global _SHUTDOWN
    log.info("=" * 70)
    log.info("  ESTrader AI v%s", VERSION)
    log.info("  US S&P 500 (US500) Spread Betting -- Capital.com")
    log.info("  Blackpool Trading Desk -- system 4 of 4")
    log.info("  Mode: %s", "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING")
    log.info("=" * 70)

    # Capital.com connector -- always connect for live price data, even in paper
    # mode. PAPER_TRADING_MODE only controls whether trades are sent live below.
    ig = CapitalComConnector()
    try:
        ig.connect()
        ig_connected = True
        log.info("Capital.com connected")
    except Exception as exc:
        log.error("Capital.com connection failed: %s -- yfinance fallback", exc)
        ig_connected = False

    feed = USDataFeed(connector=ig if ig_connected else None)
    try:
        feed.initialise()
    except Exception as exc:
        log.warning("Initial data load partial: %s -- will retry", exc)

    # Resolve any phantom PENDING verdicts that survived a restart, then start
    # a continuous watchdog so new stale rows resolve every 15 min (no restart).
    try:
        phantom_tracker.resolve_stale_pending(get_historical_price_fn=feed.get_historical_price)
        phantom_tracker.start_watchdog(get_historical_price_fn=feed.get_historical_price, interval_minutes=15)
    except Exception as _exc:
        log.warning("phantom resolve/watchdog startup failed: %s", _exc)

    # (ESTrader) No Morgan poller / confidence restore -- Morgan is removed by design.

    stanley = PaperTraderES()
    account = AccountState(capital=stanley.capital_gbp)
    # Today P&L Persist Fix (30 Jul 2026): seed the in-memory daily tally from today's
    # closed trades so a mid-day restart keeps the 'today' figure instead of resetting to 0.
    account.daily_pnl_gbp = _today_realised_pnl(TRADES_LOG)
    if account.daily_pnl_gbp:
        log.info("Restored today's realised P&L from trade log: GBP %.2f", account.daily_pnl_gbp)
    stanley.print_status()

    notify_system_startup(
        capital=stanley.capital_gbp,
        mode="PAPER" if PAPER_TRADING_MODE else "LIVE",
    )

    SHUTDOWN_FLAG.unlink(missing_ok=True)

    log.info("ESTrader AI is running. Ctrl+C to stop.")
    log.info("Dashboard: http://localhost:5007  (start dashboard_es.py separately)")

    last_candle_tick    = 0.0
    last_position_check = 0.0
    last_heartbeat      = 0.0
    last_dashboard_push = 0.0
    _force_close_done   = False

    import random
    # Stagger Capital.com API calls across systems (shared demo Z6CJSM) to avoid 429s
    STARTUP_DELAY_SECONDS = 30
    _delay = STARTUP_DELAY_SECONDS + random.uniform(0, 10)  # jitter avoids re-sync
    log.info("Staggering Capital.com requests -- waiting %.0fs before main loop", _delay)
    time.sleep(_delay)

    while not _SHUTDOWN:
        try:
            now     = time.monotonic()
            now_utc = datetime.now(timezone.utc)
            t_min   = now_utc.hour * 60 + now_utc.minute

            # Dashboard shutdown flag -- leave on disk for Galahad (see main_albiontrader pattern)
            if SHUTDOWN_FLAG.exists():
                log.info("Shutdown requested via dashboard -- stopping (flag left for watchdog)")
                break


            # Live dashboard push (all phases, every ~15s)
            if (now - last_dashboard_push) >= DASHBOARD_INTERVAL:
                _push_dashboard_live(stanley, account, ig, feed, now_utc)
                last_dashboard_push = now

            # Liveness heartbeat
            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                log.info("Heartbeat -- alive | %s UTC | phase=%s | in_trade=%s",
                         now_utc.strftime("%H:%M"), get_session_phase(now_utc), stanley.in_trade)
                last_heartbeat = now

            # Market CLOSED -- daily maintenance break (21:00-22:00 UTC) or the weekend
            # (Fri 21:00 -> Sun 22:00). Idle until the next reopen (23h US500 session).
            if get_session_phase(now_utc) == "CLOSED":
                mins = minutes_until_next_open()
                sleep_sec = max(60, min(mins * 60, HEARTBEAT_INTERVAL)) if mins else HEARTBEAT_INTERVAL
                log.info("Market closed (%s UTC) -- next reopen in %s min",
                         now_utc.strftime("%H:%M"), mins if mins else "?")
                _maybe_send_daily_summary(stanley, account)
                _interruptible_sleep(sleep_sec)
                _force_close_done = False
                continue

            # Force close at 20:55 UTC, before the daily maintenance break. Bounded to
            # the 20:55-21:00 window so it never fires during the 22:00 ASIAN reopen.
            if _FORCE_CLOSE <= t_min < _SESSION_END:
                if stanley.in_trade and not _force_close_done:
                    price  = _get_price(ig, feed)
                    gbpusd = _get_gbpusd(ig)
                    log.warning("20:55 UTC force close triggered (pre-maintenance)")
                    _close_trade(stanley, account, ig, price, "FORCE_CLOSE_2055", gbpusd)
                    _force_close_done = True
                _interruptible_sleep(30)
                continue

            # Position monitoring every 30 seconds
            if stanley.in_trade and (now - last_position_check) >= POSITION_INTERVAL:
                monitor_open_position(stanley, account, ig, feed)
                last_position_check = now

            # Candle tick every 5 minutes (during the trading session)
            if is_market_open() and (now - last_candle_tick) >= CANDLE_INTERVAL:
                run_candle_tick(feed, stanley, account, ig)
                last_candle_tick = now
            elif not is_market_open():
                # PRE_MARKET (13:30-14:30): warming up, no candle ticks / entries yet.
                _interruptible_sleep(30)
                continue

            _interruptible_sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error("Main loop error: %s", exc, exc_info=True)
            notify_system_error(str(exc)[:200])
            time.sleep(30)

    # Shutdown
    log.info("")
    log.info("=" * 70)
    log.info("  ESTrader AI -- Shutdown")
    log.info("=" * 70)
    if stanley.in_trade:
        log.warning("Position still open at shutdown -- closing paper record")
        price  = _get_price(ig, feed)
        gbpusd = _get_gbpusd(ig)
        _close_trade(stanley, account, ig, price, "SHUTDOWN", gbpusd)
    stanley.print_status()
    notify_system_shutdown(stanley.capital_gbp)
    log.info("ESTrader AI stopped cleanly.")


def _interruptible_sleep(seconds: float) -> None:
    """Sleep that responds to _SHUTDOWN flag."""
    end = time.monotonic() + seconds
    while not _SHUTDOWN and time.monotonic() < end:
        time.sleep(min(1, end - time.monotonic()))


if __name__ == "__main__":
    main()
