"""
scanner.py — Three-tier gated market scanner.
==============================================
Single heartbeat loop (60s) that independently checks which tier
it should be in and acts accordingly. All existing alert logic is
unchanged — only the gating around it is new.

TIER 1 — WEEKLY GATE  (Friday post-close, ~17:00 ET)
  Runs the SPY regime check once per week.
  Sends a Telegram summary of the weekly regime reading.
  Sets market_active flag in regime_state.json.
  If not bull → silent message sent, bot quiet for the week.

TIER 2 — DAILY GATE  (each weekday, pre-market ~07:00 ET)
  Only runs if market_active is True.
  Runs a fresh SPY regime check using prior day's confirmed close.
  Sets day_active flag in regime_state.json.
  Sends a brief pre-market Telegram note (active or skip).

TIER 3 — INTRADAY UNIVERSE SCAN  (every SCAN_INTERVAL seconds)
  Only runs if both market_active AND day_active are True.
  Existing Setup A logic unchanged — 8 checks, same alerts, same stops.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from telegram import Bot
from telegram.constants import ParseMode

import watchlist as wl_store
from analysis import fetch_and_analyse, AnalysisResult, _score_label
from regime import get_regime_context, TARGET_REGIMES, REGIME_LABELS
from regime_state import (
    save_weekly, save_daily,
    is_market_active, is_day_active,
    has_weekly_run_this_week, has_daily_run_today,
    load_state,
)

logger = logging.getLogger(__name__)

# ── Timezone ───────────────────────────────────────────────────────────────────
EASTERN = pytz.timezone("US/Eastern")

# ── Configuration ──────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS   = int(os.environ.get("SCAN_INTERVAL",         900))  # 15 min default
ALERT_COOLDOWN_HOURS    = int(os.environ.get("ALERT_COOLDOWN_HOURS",    4))
PARTIAL_ALERT_MIN_SCORE = int(os.environ.get("PARTIAL_ALERT_MIN_SCORE", 5))
UNIVERSE_MODE           = os.environ.get("UNIVERSE_MODE", "false").lower() == "true"

# Cap on alerts sent per scan cycle (universe mode can surface far more
# than this many qualifying tickers — only the top-ranked ones are sent)
MAX_FULL_ALERTS_PER_SCAN  = int(os.environ.get("MAX_FULL_ALERTS_PER_SCAN",  5))
MAX_WATCH_ALERTS_PER_SCAN = int(os.environ.get("MAX_WATCH_ALERTS_PER_SCAN", 5))

# Per-ticker pacing inside the scan loop (seconds)
SCAN_PER_TICKER_SLEEP = float(os.environ.get("SCAN_PER_TICKER_SLEEP", 1.0))

# Heartbeat — how often the loop wakes to check which tier applies
_HEARTBEAT_SECONDS = 60


# ── Time helpers ───────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(EASTERN)


def _is_friday_post_close() -> bool:
    """True on Fridays after 17:00 ET — weekly gate window."""
    now = _now_et()
    return now.weekday() == 4 and now.hour >= 17   # Friday = 4


def _is_pre_market_check_time() -> bool:
    """
    True Mon–Fri between 07:00 and 07:59 ET.
    Pre-market window for the daily gate check.
    Using prior-close data so this is always based on confirmed prices.
    """
    now = _now_et()
    return now.weekday() < 5 and now.hour == 7


def _is_market_hours() -> bool:
    """True during regular US equity market hours Mon–Fri, 09:30–16:00 ET."""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


# ── Message builders ───────────────────────────────────────────────────────────

def _build_weekly_active_msg(ctx: dict) -> str:
    target_labels = " / ".join(
        REGIME_LABELS[r] for r in sorted(TARGET_REGIMES) if r in REGIME_LABELS
    )
    return (
        f"📅 *Weekly Regime Check*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Market is BULL — scanning active this week*\n"
        f"🌐 Regime: `{ctx.get('label', '—')}`\n\n"
        f"📊 *SPY Snapshot*\n"
        f"  • Close:    `${ctx.get('spy_close', 0):.2f}`\n"
        f"  • SMA50:    `${ctx.get('spy_sma50', 0):.2f}`\n"
        f"  • SMA200:   `${ctx.get('spy_sma200', 0):.2f}`\n"
        f"  • ATR Rank: `{ctx.get('atr_rank_pct', 0):.1f}th pct`\n\n"
        f"_Daily pre-market checks will run Mon–Fri._\n"
        f"_Alerts fire only on `{target_labels}` days._"
    )


def _build_weekly_silent_msg(ctx: dict) -> str:
    return (
        f"📅 *Weekly Regime Check*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⛔ *Market is NOT BULL — bot silent this week*\n"
        f"🌐 Regime: `{ctx.get('label', '—')}`\n\n"
        f"📊 *SPY Snapshot*\n"
        f"  • Close:    `${ctx.get('spy_close', 0):.2f}`\n"
        f"  • SMA50:    `${ctx.get('spy_sma50', 0):.2f}`\n"
        f"  • SMA200:   `${ctx.get('spy_sma200', 0):.2f}`\n\n"
        f"_No daily checks or ticker scans until next Friday._\n"
        f"_Use /refresh to force a regime re-check if conditions change._"
    )


def _build_daily_active_msg(ctx: dict) -> str:
    return (
        f"🌅 *Pre-Market Check — Scanning Today*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Regime: `{ctx.get('label', '—')}`\n"
        f"_Intraday universe scan active — every "
        f"{SCAN_INTERVAL_SECONDS // 60} min during market hours._"
    )


def _build_daily_skip_msg(ctx: dict) -> str:
    return (
        f"🌅 *Pre-Market Check — No Scan Today*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏭️ Regime: `{ctx.get('label', '—')}`\n"
        f"_Market is bull but today is not a target regime day._\n"
        f"_No ticker scans until tomorrow's pre-market check._"
    )


def _check_row(label: str, passed: bool, detail: str = "") -> str:
    icon   = "✅" if passed else "❌"
    suffix = f"  `{detail}`" if detail else ""
    return f"  {icon} {label}{suffix}"


def _build_full_alert(result: AnalysisResult, regime_label: str) -> str:
    a  = result.checks
    s  = result.stops
    ma = ", ".join(result.triggered_mas) if result.triggered_mas else "—"
    vr = (f"{result.volume_current / result.volume_ma20:.1f}× avg"
          if result.volume_ma20 and result.volume_ma20 > 0 else "n/a")

    checks = "\n".join([
        _check_row("Uptrend  (Price > 200 SMA)",       a.uptrend),
        _check_row("SMA200 Slope  (rising 20 bars)",   a.sma200_slope),
        _check_row("MA Proximity  (low ≤ 1.5%)",       a.ma_proximity,   ma),
        _check_row("Bullish Candle  (green + upper ½)", a.bullish_candle),
        _check_row("Volume  (≥ 20-day avg)",            a.volume_ok,      vr),
        _check_row("EMA Slope  (62 EMA rising)",        a.ema_slope_ok),
        _check_row("Clean Structure  (5-bar hold)",     a.clean_structure),
        _check_row("Not Overextended  (≤ 10% > SMA)",  a.not_overextended),
    ])

    return (
        f"🚨 *TRADE ALERT — {result.ticker}* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Entry Price:*  `${result.current_price:.2f}`\n"
        f"📊 *Signal Quality:*  {_score_label(a.score)}  `{a.score}/8`\n"
        f"🌐 *Regime:*  `{regime_label}`\n\n"
        f"📐 *Moving Averages*\n"
        f"  • 200 SMA → `${result.sma_200:.2f}`\n"
        f"  • 62  EMA → `${result.ema_62:.2f}`\n"
        f"  • 79  EMA → `${result.ema_79:.2f}`\n"
        f"  • ATR 14  → `${result.atr_14:.2f}`\n\n"
        f"🎯 *MA Touch:*  `{ma}`\n\n"
        f"🛡️ *Stop Loss Levels*\n"
        f"  • Hard stop → `${s.hard_stop:.2f}`  _(79 EMA − 1× ATR)_\n"
        f"  • Primary   → `${s.primary_stop:.2f}`  _(close below 79 EMA)_\n"
        f"  • Breakdown → `${s.catastrophic_stop:.2f}`  _(200 SMA breach)_\n"
        f"  • 2R Target → `${s.partial_target:.2f}`\n"
        f"  • Trail at  → `${s.trail_activation:.2f}`  _(+3%)_\n"
        f"  • Risk:  `{s.risk_pct:.2f}%` of entry\n\n"
        f"✅ *Entry Checks  [{a.score}/8]*\n"
        f"{checks}\n\n"
        f"🕐 `{result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Exit: daily close below 79 EMA._"
    )


def _build_watch_alert(result: AnalysisResult, regime_label: str) -> str:
    a  = result.checks
    ma = ", ".join(result.triggered_mas) if result.triggered_mas else "—"
    failing = [
        lbl for check, lbl in [
            (a.sma200_slope,    "SMA200 slope rising"),
            (a.bullish_candle,  "bullish candle confirmation"),
            (a.volume_ok,       "above-average volume"),
            (a.ema_slope_ok,    "62 EMA slope rising"),
            (a.clean_structure, "clean 5-bar EMA structure"),
            (a.not_overextended,"price not overextended"),
        ] if not check
    ]
    return (
        f"👀 *WATCHING — {result.ticker}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Price:*  `${result.current_price:.2f}`   "
        f"🌐 `{regime_label}`\n"
        f"📊 Score: `{a.score}/8`   🎯 Touch: `{ma}`\n\n"
        f"⏳ *Still needed:*\n"
        + "\n".join(f"  ⏳ {f}" for f in failing) +
        f"\n\n🕐 `{result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── Cooldown ───────────────────────────────────────────────────────────────────

def _cooldown_expired(last_ts_str: str | None) -> bool:
    if last_ts_str is None:
        return True
    try:
        ts = datetime.fromisoformat(last_ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts > timedelta(hours=ALERT_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return True


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run_scanner_loop(bot: Bot, chat_id: str) -> None:
    """
    Single 60-second heartbeat.  Each wake-up independently decides which
    tier applies:
      • Friday post-close  → weekly regime check (once per week)
      • Pre-market 07:xx   → daily regime check  (once per weekday, if active)
      • Market hours       → ticker scan          (every SCAN_INTERVAL, if both active)
    """
    logger.info(
        "Scanner started — heartbeat=%ds  scan_interval=%ds  cooldown=%dh",
        _HEARTBEAT_SECONDS, SCAN_INTERVAL_SECONDS, ALERT_COOLDOWN_HOURS,
    )

    last_intraday_scan: datetime | None = None

    while True:
        try:
            # ── TIER 1: Weekly regime gate (Friday post-close) ─────────────────
            if _is_friday_post_close() and not has_weekly_run_this_week():
                await _run_weekly_check(bot, chat_id)

            # ── TIER 2: Daily regime gate (pre-market Mon–Fri) ─────────────────
            elif (
                _is_pre_market_check_time()
                and not has_daily_run_today()
                and is_market_active()
            ):
                await _run_daily_check(bot, chat_id)

            # ── TIER 3: Intraday universe scan ─────────────────────────────────
            elif _is_market_hours() and is_day_active():
                now = datetime.now(timezone.utc)
                due = (
                    last_intraday_scan is None
                    or (now - last_intraday_scan).total_seconds() >= SCAN_INTERVAL_SECONDS
                )
                if due:
                    await _run_ticker_scan(bot, chat_id)
                    last_intraday_scan = datetime.now(timezone.utc)

        except Exception as exc:
            logger.exception("Heartbeat error: %s", exc)

        await asyncio.sleep(_HEARTBEAT_SECONDS)


# ── Tier 1: Weekly check ───────────────────────────────────────────────────────

async def _run_weekly_check(bot: Bot, chat_id: str) -> None:
    """Friday post-close: classify regime, set market_active, notify."""
    logger.info("TIER 1 — Weekly regime check starting")
    ctx = get_regime_context()
    if not ctx:
        logger.warning("Weekly check: could not fetch regime context")
        return

    save_weekly(ctx)
    active = ctx.get("regime") in {"BULL_TREND", "BULL_HIGH_VOL", "CORRECTION"}

    if active:
        msg = _build_weekly_active_msg(ctx)
        logger.info("Weekly check: %s — market ACTIVE this week", ctx.get("regime"))
    else:
        msg = _build_weekly_silent_msg(ctx)
        logger.info("Weekly check: %s — market SILENT this week", ctx.get("regime"))

    await _send(bot, chat_id, msg)


# ── Tier 2: Daily check ────────────────────────────────────────────────────────

async def _run_daily_check(bot: Bot, chat_id: str) -> None:
    """Pre-market: check today's regime, set day_active, notify."""
    logger.info("TIER 2 — Daily pre-market regime check")
    ctx = get_regime_context()
    if not ctx:
        logger.warning("Daily check: could not fetch regime context")
        return

    save_daily(ctx)
    day_active = ctx.get("regime") in TARGET_REGIMES

    if day_active:
        msg = _build_daily_active_msg(ctx)
        logger.info("Daily check: %s — scans ACTIVE today", ctx.get("regime"))
    else:
        msg = _build_daily_skip_msg(ctx)
        logger.info("Daily check: %s — scans SKIPPED today", ctx.get("regime"))

    await _send(bot, chat_id, msg)


# ── Tier 3: Universe / watchlist scan ─────────────────────────────────────────

async def _run_ticker_scan(bot: Bot, chat_id: str) -> None:
    """
    Run Setup A across the full universe or watchlist.

    Two-pass design (needed because universe mode can scan 1000+ tickers):
      Pass 1 — score every ticker, update its persisted state, and bucket
               cooldown-eligible candidates into "full signal" (8/8) and
               "watching" (partial score) groups.
      Pass 2 — rank each group (volume surge ratio, then MA confluence
               count, both descending) and send alerts for only the
               top MAX_FULL_ALERTS_PER_SCAN / MAX_WATCH_ALERTS_PER_SCAN.
    """
    # Determine what to scan
    if UNIVERSE_MODE:
        from universe import load_candidates
        candidates = load_candidates()
        if not candidates:
            logger.info("Universe mode: no candidates — run /refreshuniverse")
            return
        watchlist = {t: {} for t in candidates}
    else:
        watchlist = wl_store.load()
        if not watchlist:
            logger.info("Watchlist empty — nothing to scan")
            return

    # Get current regime label for alert messages
    state = load_state()
    regime_label = state.get("daily", {}).get("label", "Unknown")

    logger.info("TIER 3 — Scanning %d ticker(s) | regime: %s",
                len(watchlist), regime_label)

    full_candidates: list[AnalysisResult] = []
    watch_candidates: list[AnalysisResult] = []

    # ── Pass 1: score every ticker ──────────────────────────────────────────
    for ticker, ticker_state in list(watchlist.items()):
        result = await _scan_ticker(
            watchlist, ticker, ticker_state,
            full_candidates, watch_candidates,
        )
        if result is not None:
            await asyncio.sleep(SCAN_PER_TICKER_SLEEP)  # keep event loop responsive

    logger.info(
        "Pass 1 complete — %d full-signal candidate(s), %d watching candidate(s)",
        len(full_candidates), len(watch_candidates),
    )

    # ── Pass 2: rank and alert on the top N of each group ───────────────────
    full_candidates.sort(key=_rank_key, reverse=True)
    watch_candidates.sort(key=_rank_key, reverse=True)

    top_full  = full_candidates[:MAX_FULL_ALERTS_PER_SCAN]
    top_watch = watch_candidates[:MAX_WATCH_ALERTS_PER_SCAN]

    for result in top_full:
        await _send(bot, chat_id, _build_full_alert(result, regime_label))
        wl_store.update_ticker_state(
            watchlist, result.ticker,
            last_alert_ts=result.timestamp.isoformat(),
        )
        logger.info(
            "ALERT: %s  score=8/8  vol_ratio=%.2f  MA=%s",
            result.ticker, result.volume_ratio or 0.0, result.triggered_mas,
        )

    for result in top_watch:
        await _send(bot, chat_id, _build_watch_alert(result, regime_label))
        logger.info(
            "WATCHING: %s  score=%d/8  vol_ratio=%.2f",
            result.ticker, result.checks.score, result.volume_ratio or 0.0,
        )

    if len(full_candidates) > MAX_FULL_ALERTS_PER_SCAN:
        logger.info(
            "%d additional 8/8 signal(s) suppressed by top-%d cap",
            len(full_candidates) - MAX_FULL_ALERTS_PER_SCAN, MAX_FULL_ALERTS_PER_SCAN,
        )
    if len(watch_candidates) > MAX_WATCH_ALERTS_PER_SCAN:
        logger.info(
            "%d additional watching candidate(s) suppressed by top-%d cap",
            len(watch_candidates) - MAX_WATCH_ALERTS_PER_SCAN, MAX_WATCH_ALERTS_PER_SCAN,
        )

    logger.info("Scan complete")


def _rank_key(result: AnalysisResult) -> tuple[float, int]:
    """
    Ranking key for breaking ties among same-score candidates.
    Sorted descending: higher volume surge ratio first, then more
    moving-average confluence (more MAs within proximity).
    """
    return (result.volume_ratio or 0.0, len(result.triggered_mas))


async def _scan_ticker(
    watchlist: dict,
    ticker: str, state: dict,
    full_candidates: list, watch_candidates: list,
) -> Optional[AnalysisResult]:
    """
    Fetch + analyse one ticker, persist its latest state, and -- if it
    qualifies and isn't on cooldown -- add it to the appropriate
    candidate bucket for ranking in pass 2. Returns the result (or None
    on a data-fetch error, so the caller can skip the pacing sleep).
    """
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, fetch_and_analyse, ticker)

    if result.error:
        logger.warning("Skipping %s -- %s", ticker, result.error)
        return None

    a = result.checks

    wl_store.update_ticker_state(
        watchlist, ticker,
        last_price = result.current_price,
        trend      = "up" if a.uptrend else "down",
        last_score = a.score,
    )

    cooldown_ok = _cooldown_expired(state.get("last_alert_ts"))

    if a.all_pass and cooldown_ok:
        full_candidates.append(result)

    elif a.all_pass and not cooldown_ok:
        logger.info("SUPPRESSED (cooldown): %s", ticker)

    elif (a.score >= PARTIAL_ALERT_MIN_SCORE
          and a.ma_proximity and a.uptrend and cooldown_ok):
        watch_candidates.append(result)

    else:
        logger.debug("%s -- no signal. score=%d/8", ticker, a.score)

    return result


async def _send(bot: Bot, chat_id: str, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text,
                               parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
xc)
