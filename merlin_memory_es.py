"""
merlin_memory_es.py -- Merlin's Memory (ESTrader pilot, 27 Jul 2026)

Arthur's EPISODIC MEMORY of his own past trading decisions and outcomes on US500.
Regenerated FRESH from live CSV data at every Arthur consultation and injected into
his prompt as a "TRADING MEMORY" section (see main_estrader.run_candle_tick ->
agent_brain_es.get_trading_decision memory_context).

Three memory types:
  1. RECENT COMPLETED TRADES   -- last 10 closed trades + an auto-generated lesson.
  2. RECENT STAY-OUT DECISIONS -- last 10 FAIR (benchmark-FLAT) phantom rows.
  3. SELF-OBSERVED PATTERNS    -- simple stats, reported only when n >= 3.

Total memory is capped at ~600 tokens; when over budget the priority is
Type 3 (patterns) > Type 1 (trades) > Type 2 (phantom) -- most actionable first.

All times UTC. Read-only over the CSVs; never raises out of get_memory_summary().
This is the ONE genuinely new subsystem on ESTrader (Morgan/Guinevere are removed).
"""
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ESTrader.MerlinMemory")

BASE_DIR      = Path(__file__).resolve().parent
LOG_DIR       = BASE_DIR / "logs"
TRADES_CSV    = LOG_DIR / "es_trades.csv"
PHANTOM_CSV   = LOG_DIR / "es_phantom_trades.csv"
MEMORY_LOG    = LOG_DIR / "es_memory_log.csv"
PATTERNS_SEEN = LOG_DIR / "memory_patterns_seen.json"

MAX_TOKENS    = 600       # hard cap on the injected memory section
RECENT_N      = 10        # how many recent rows per memory type
MIN_PATTERN_N = 3         # never report a pattern with fewer than this many examples

MEMORY_LOG_HEADERS = [
    "timestamp", "memory_tokens_used", "trades_included",
    "phantoms_included", "patterns_found", "arthur_confidence",
]

# Stats from the most recent get_memory_summary() build (same process = the engine).
_last_build = {"tokens": 0, "trades": 0, "phantoms": 0, "patterns": 0, "pattern_keys": []}


# ── small helpers ─────────────────────────────────────────────────────────────

def _toks(text: str) -> int:
    """Rough token estimate (~4 chars/token). Deliberately conservative."""
    return max(1, len(text) // 4)


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return [r for r in csv.DictReader(fh) if any((v or "").strip() for v in r.values())]
    except Exception as exc:  # noqa: BLE001
        log.warning("memory: read %s failed: %s", path.name, exc)
        return []


# ── MEMORY TYPE 1 -- recent completed trades ──────────────────────────────────

def _trade_lesson(row: dict) -> str:
    """Auto-generate a one-line lesson for a completed trade row."""
    reason    = (row.get("exit_reason") or "").upper()
    direction = (row.get("direction") or "").upper()
    pnl_pts   = _f(row.get("points_gained")) or 0.0
    exit_px   = _f(row.get("exit_price_usd"))
    p30       = _f(row.get("price_30m_after"))
    daily_ssl = (row.get("exit_daily_ssl") or "").upper()

    # WIN locked by the Profit Protection Ladder (a profitable stop exit).
    if "STOP" in reason and pnl_pts > 0:
        return "Profit Protection Ladder locked gains successfully."

    if "STOP" in reason:
        flipped = (direction == "LONG" and daily_ssl == "BEAR") or \
                  (direction == "SHORT" and daily_ssl == "BULL")
        if flipped:
            return ("Daily SSL flipped against position during trade. "
                    "Consider exiting earlier when daily SSL flips.")
        return "Stop loss hit -- signal did not develop as expected."

    if reason.startswith("ARTHUR_EXIT"):
        if p30 is not None and exit_px is not None:
            # "worse than exit" = price kept moving AGAINST the closed position.
            if direction == "LONG":
                continued_against = p30 < exit_px       # price fell further after a long exit
            else:
                continued_against = p30 > exit_px       # price rose further after a short exit
            if continued_against:
                return ("Exit was well-timed -- price continued against the position "
                        "after Arthur exited.")
            recovered = abs(p30 - exit_px)
            return ("Exit may have been premature -- price recovered %.1fpts within "
                    "30 minutes of exit." % recovered)
        return "Arthur exit -- post-exit recovery data still filling in."

    if "FORCE_CLOSE" in reason:
        return "Force-closed before the daily maintenance break (not a signal exit)."
    return "Closed -- no specific lesson."


def _build_type1(rows: list) -> list:
    """Return a list of text blocks, newest first, for recent trades."""
    out = []
    for row in reversed(rows[-RECENT_N:]):
        date  = row.get("date") or (row.get("entry_time", "")[:10])
        drn   = (row.get("direction") or "?").upper()
        entry = _f(row.get("entry_price_usd"))
        entry_s = f"{entry:,.1f}" if entry is not None else "?"
        reason  = row.get("exit_reason") or "?"
        pnl_pts = _f(row.get("points_gained"))
        pnl_gbp = _f(row.get("pnl_gbp"))
        pnl_s   = f"{pnl_pts:+.1f}" if pnl_pts is not None else "?"
        gbp_s   = f"£{pnl_gbp:+.2f}" if pnl_gbp is not None else "?"
        p30 = row.get("price_30m_after") or "--"
        p60 = row.get("price_60m_after") or "--"
        block = (f"{date} {drn} entry {entry_s} -> {reason} {pnl_s}pts ({gbp_s})\n"
                 f"  Recovery: price {p30} at 30min, {p60} at 60min\n"
                 f"  Lesson: {_trade_lesson(row)}")
        out.append(block)
    return out


# ── MEMORY TYPE 2 -- recent FAIR phantom stay-outs ────────────────────────────

_VERDICT_EXPL = {
    "CORRECT": "Right to stay out -- price moved against the signal direction.",
    "WRONG":   "Missed opportunity -- price moved in the signal direction.",
    "NEUTRAL": "Inconclusive -- move was too small to classify.",
}


def _build_type2(rows: list):
    fair = [r for r in rows
            if (r.get("fair_comparison") or "").upper() == "TRUE"
            and (r.get("verdict") or "").upper() in _VERDICT_EXPL]
    out = []
    for row in reversed(fair[-RECENT_N:]):
        date = (row.get("timestamp") or "")[:10]
        drn  = (row.get("direction_blocked") or "?").upper()
        conf = row.get("confidence") or "?"
        pnl1 = _f(row.get("pnl_1hr"))
        pnl_s = f"{pnl1:+.1f}" if pnl1 is not None else "--"
        verd = (row.get("verdict") or "").upper()
        block = (f"{date} stayed out of {drn} (conf {conf})\n"
                 f"  Outcome at 1hr: {pnl_s} -> {verd}\n"
                 f"  {_VERDICT_EXPL.get(verd, '')}")
        out.append(block)
    return out, len(fair)


# ── MEMORY TYPE 3 -- self-observed patterns (n >= 3 only) ─────────────────────

def _is_win(r):
    p = _f(r.get("points_gained"))
    return p is not None and p > 0


def _build_type3(trades: list, phantoms: list):
    """Return (list_of_pattern_lines, list_of_pattern_keys). Only n>=3 patterns."""
    lines, keys = [], []

    # PATTERN A -- session timing by direction + phase
    for drn in ("LONG", "SHORT"):
        buckets = {}
        for r in trades:
            if (r.get("direction") or "").upper() != drn:
                continue
            ph = (r.get("session_phase") or "?").upper()
            buckets.setdefault(ph, []).append(r)
        for ph, rs in buckets.items():
            if len(rs) >= MIN_PATTERN_N:
                wins = sum(1 for r in rs if _is_win(r))
                pct = round(100 * wins / len(rs))
                tag = "Good signal timing historically." if pct >= 55 else "Apply extra caution."
                lines.append(f"{drn}s entered in {ph} have won {pct}% of the last "
                             f"{len(rs)} trades. {tag}")
                keys.append(f"session:{drn}:{ph}")

    # PATTERN B -- LONG entries at extended (above-median) price hitting stop
    longs = [r for r in trades if (r.get("direction") or "").upper() == "LONG"
             and _f(r.get("entry_price_usd")) is not None]
    if len(longs) >= MIN_PATTERN_N:
        entries = sorted(_f(r.get("entry_price_usd")) for r in longs)
        median = entries[len(entries) // 2]
        extended = [r for r in longs if _f(r.get("entry_price_usd")) > median]
        stopped = [r for r in extended if "STOP" in (r.get("exit_reason") or "").upper()
                   and not _is_win(r)]
        if len(extended) >= MIN_PATTERN_N and len(stopped) >= MIN_PATTERN_N:
            lines.append(f"LONG entries above {median:,.0f} have hit stop loss "
                         f"{len(stopped)} of the last {len(extended)} times. Be cautious "
                         f"entering LONGs at extended price levels.")
            keys.append("level:LONG:extended")

    # PATTERN C -- under-trading strong 3-SSL-agree setups (phantom WRONG stay-outs)
    for drn in ("LONG", "SHORT"):
        want = "BULL" if drn == "LONG" else "BEAR"
        agree = [r for r in phantoms
                 if (r.get("fair_comparison") or "").upper() == "TRUE"
                 and (r.get("ssl_daily") or "").upper() == want
                 and (r.get("ssl_1hr") or "").upper() == want
                 and (r.get("ssl_5min") or "").upper() == want
                 and (r.get("direction_blocked") or "").upper() == drn]
        wrong = [r for r in agree if (r.get("verdict") or "").upper() == "WRONG"]
        if len(agree) >= MIN_PATTERN_N and len(wrong) >= MIN_PATTERN_N:
            lines.append(f"When Daily+1hr+5min SSL all agree {drn}, your stay-outs have been "
                         f"WRONG {len(wrong)} of the last {len(agree)} times. This is a strong "
                         f"setup you may be UNDER-TRADING.")
            keys.append(f"undertrade:{drn}")

    # PATTERN D -- ARTHUR_EXIT timing by direction (recovered vs continued)
    for drn in ("LONG", "SHORT"):
        exits = [r for r in trades
                 if (r.get("exit_reason") or "").upper().startswith("ARTHUR_EXIT")
                 and (r.get("direction") or "").upper() == drn
                 and _f(r.get("price_30m_after")) is not None
                 and _f(r.get("exit_price_usd")) is not None]
        if len(exits) >= MIN_PATTERN_N:
            recovered = 0
            for r in exits:
                p30 = _f(r.get("price_30m_after")); ex = _f(r.get("exit_price_usd"))
                if (drn == "LONG" and p30 > ex) or (drn == "SHORT" and p30 < ex):
                    recovered += 1
            if recovered * 2 >= len(exits):   # majority recovered -> exiting too early
                lines.append(f"Your last {len(exits)} ARTHUR_EXITs on {drn} trades showed price "
                             f"RECOVERING after exit ({recovered}/{len(exits)}). Consider holding longer.")
                keys.append(f"exit:{drn}:recover")
            else:
                lines.append(f"Your last {len(exits)} ARTHUR_EXITs on {drn} trades showed price "
                             f"CONTINUING against the position after exit -- exits are well-timed.")
                keys.append(f"exit:{drn}:continue")

    return lines, keys


# ── Assemble the injected memory summary ──────────────────────────────────────

def _wrap(title: str, blocks: list, empty: str) -> str:
    body = "\n".join(blocks) if blocks else empty
    return f"{title}:\n{body}"


def get_memory_summary() -> str:
    """Build Arthur's TRADING MEMORY section, capped at ~600 tokens. Never raises."""
    try:
        trades   = _read_csv(TRADES_CSV)
        phantoms = _read_csv(PHANTOM_CSV)

        t1 = _build_type1(trades)
        t2, fair_count = _build_type2(phantoms)
        t3, pkeys = _build_type3(trades, phantoms)

        # Insufficient-history fallbacks (per brief).
        t1_empty = ("Insufficient trade history -- building memory. Trade on indicators only."
                    if len(trades) < MIN_PATTERN_N else "None yet.")
        t2_empty = ("Insufficient phantom history -- building memory."
                    if fair_count < MIN_PATTERN_N else "None yet.")
        t3_empty = "Patterns still developing -- need more trading history."

        header = ("=" * 60 + "\nTRADING MEMORY -- YOUR RECENT DECISIONS ON US500\n" + "=" * 60)
        footer = ("Use this memory to inform your current decision. Learn from past "
                  "mistakes. Reinforce what has worked.\n" + "=" * 60)

        sec_patterns = _wrap("PATTERNS OBSERVED IN YOUR TRADING", t3, t3_empty)
        sec_trades   = _wrap("RECENT COMPLETED TRADES (last %d)" % min(len(t1), RECENT_N), t1, t1_empty)
        sec_phantom  = _wrap("RECENT STAY-OUT DECISIONS (fair comparison, last %d)"
                             % min(len(t2), RECENT_N), t2, t2_empty)

        # Budget: header+footer fixed; fill Type3 > Type1 > Type2 within MAX_TOKENS.
        fixed = _toks(header) + _toks(footer)
        budget = MAX_TOKENS - fixed
        chosen, used = [], 0
        for sec in (sec_patterns, sec_trades, sec_phantom):
            c = _toks(sec)
            if used + c <= budget:
                chosen.append(sec); used += c
            else:
                remaining = budget - used
                if remaining > 20:
                    head, _, rest = sec.partition("\n")
                    kept = [head]
                    for ln in rest.split("\n"):
                        if _toks("\n".join(kept + [ln])) > remaining:
                            kept.append("  ... (older entries omitted to fit memory budget)")
                            break
                        kept.append(ln)
                    chosen.append("\n".join(kept)); used = budget
                break

        summary = "\n\n".join([header] + chosen + [footer])

        _last_build.update({
            "tokens": fixed + used,
            "trades": min(len(t1), RECENT_N),
            "phantoms": min(len(t2), RECENT_N),
            "patterns": len(t3),
            "pattern_keys": pkeys,
        })
        return summary
    except Exception as exc:  # noqa: BLE001 -- memory must never break the tick
        log.warning("get_memory_summary failed: %s", exc)
        _last_build.update({"tokens": 0, "trades": 0, "phantoms": 0, "patterns": 0, "pattern_keys": []})
        return ""


# ── Logging + stats + Percival pattern-detection ──────────────────────────────

def log_consultation(arthur_confidence=None) -> None:
    """Append one row to es_memory_log.csv after an Arthur consult (Gaius can then
    assess whether memory improves decision quality over time). Never raises."""
    try:
        new = not MEMORY_LOG.exists()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(MEMORY_LOG, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=MEMORY_LOG_HEADERS)
            if new:
                w.writeheader()
            w.writerow({
                "timestamp":          datetime.now(timezone.utc).isoformat(),
                "memory_tokens_used": _last_build.get("tokens", 0),
                "trades_included":    _last_build.get("trades", 0),
                "phantoms_included":  _last_build.get("phantoms", 0),
                "patterns_found":     _last_build.get("patterns", 0),
                "arthur_confidence":  arthur_confidence if arthur_confidence is not None else "",
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("log_consultation failed: %s", exc)


def get_memory_stats() -> dict:
    """Latest memory stats for the dashboard / Archie brief (separate process): read
    the last es_memory_log.csv row; fall back to zeros if none yet."""
    rows = _read_csv(MEMORY_LOG)
    if rows:
        last = rows[-1]
        return {
            "trades":   int(_f(last.get("trades_included")) or 0),
            "phantoms": int(_f(last.get("phantoms_included")) or 0),
            "patterns": int(_f(last.get("patterns_found")) or 0),
            "tokens":   int(_f(last.get("memory_tokens_used")) or 0),
        }
    return {"trades": 0, "phantoms": 0, "patterns": 0, "tokens": 0}


def pop_new_patterns() -> list:
    """Return pattern keys that reached n>=3 for the FIRST time since last call,
    persisting the seen set (logs/memory_patterns_seen.json). Used by main to fire a
    Percival "memory pattern detected" notification once per new pattern. Never raises."""
    try:
        current = set(_last_build.get("pattern_keys", []))
        seen = set()
        if PATTERNS_SEEN.exists():
            seen = set(json.loads(PATTERNS_SEEN.read_text(encoding="utf-8")))
        new = sorted(current - seen)
        if new:
            PATTERNS_SEEN.write_text(json.dumps(sorted(seen | current)), encoding="utf-8")
        return new
    except Exception as exc:  # noqa: BLE001
        log.warning("pop_new_patterns failed: %s", exc)
        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(get_memory_summary() or "(no memory yet)")
    print("\nSTATS:", get_memory_stats())
