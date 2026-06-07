"""
scanner.py — Asynchronous background market-scanning loop.

Two distinct modes driven by a single 60-second heartbeat loop:

POST-CLOSE MODE  (once per trading day, after US market close)
─────────────────────────────────────────────────────────────
  • Fires once the US/Eastern clock passes 17:00 ET (DST-aware).
  • Runs the regime check against confirmed daily SPY closing data.
  • Saves the result to regime_state.json via regime_state module.
  • If regime is a target: triggers automatic universe refresh, sends
    a confirmation Telegram message.
  • If regime is not a target: sends a skip Telegram message.
  • Will not fire again until the next calendar day (US/Eastern).

INTRADAY MODE  (next trading day, during market hours)
──────────────────────────────────────────────────────
  • Reads yesterday's saved regime_state.json.
  • If is_target is True, scans all tickers every INTRADAY_SCAN_INTERVAL
    seconds (default 900 = 15 min) during market hours (09:30–16:00 ET,
    DST-aware).
  • If is_target is False, does nothing until the next post-close check.
  • Full alert / watch alert logic unchanged.

DST handling:
  All market open/close times are derived from the US/Eastern timezone via
  pytz so EDT (UTC-4) and EST (UTC-5) are handled automatically year-round.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import pytz
from telegram import Bot
from telegram.constants import ParseMode

import watchlist as wl_store
from analysis import fetch_and_analyse, AnalysisResult, _score_label
from regime import is_target_regime, get_regime_context, TARGET_REGIMES, REGIME_LABELS
from regime_state import (
    save_regime_state,
    load_regime_state,
    is_today_target,
    has_run_today,
)

logger = logging.getLogger(__name__)

# ── Timezone ───────────────────────────────────────────────────────────────────
EASTERN = pytz.timezone("US/Eastern")

# ── Configuration ──────────────────────────────────────────────────────────────
# How often (seconds) intraday ticker scans run during market hours.
INTRADAY_SCAN_INTERVAL = int(os.environ.get("INTRADAY_SCAN_INTERVAL", 900))

# Alert cooldown — suppress repeated alerts for the same ticker.
ALERT_COOLDOWN_HOURS = int(os.environ.get("ALERT_COOLDOWN_HOURS", 4))

# Minimum score for a "watching" partial alert.
PARTIAL_ALERT_MIN_SCORE = int(os.environ.get("PARTIAL_ALERT_MIN_SCORE", 5))

# Regime filter toggle.
REGIME_FILTER_ENABLED = os.environ.get("REGIME_FILTER", "true").lower() == "true"

# Universe mode — scan pre-screened candidates instead of manual watchlist.
UNIVERSE_MODE = os.environ.get("UNIVERSE_MODE", "false").lower() == "true"

# How often the heartbeat loop wakes to check which mode applies (seconds).
_HEARTBEAT_INTERVAL = 60


# ── Market time helpers ────────────────────────────────────────────────────────

def _now_eastern() -> datetime:
    """Current time in US/Eastern (DST-aware)."""
    return datetime.now(EASTERN)


def _is_post_close() -> bool:
    """
    True on weekdays once the US market has closed for the day.
    Market close = 17:00 ET. DST handled automatically via pytz.
    """
    now_et = _now_eastern()
    if now_et.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return now_et.hour >= 17


def _is_market_hours() -> bool:
    """
    True during regular US equity market hours on weekdays.
    09:30–16:00 ET. DST handled automatically via pytz.
    """
    now_et = _now_eastern()
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close


# ── Message builders ───────────────────────────────────────────────────────────

def _check_row(label: str, passed: bool, detail: str = "") -> str:
    icon   = "✅" if passed else "❌"
    suffix = f"  `{detail}`" if detail else ""
    return f"  {icon} {label}{suffix}"


def _build_full_alert(result: AnalysisResult, persisted_regime: dict | None = None) -> str:
    c = result.checks
    s = result.stops
    ma_hits = ", ".join(result.triggered_mas) if result.triggered_mas else "—"

    vol_ratio = (
        f"{result.volume_current / result.volume_ma20:.1f}x avg"
        if result.volume_ma20 and result.volume_ma20 > 0
        else "n/a"
    )

    score_label = _score_label(c.score)

    checks_block = "\n".join([
        _check_row("Uptrend  (Price > 200 SMA)",        c.uptrend),
        _check_row("SMA200 Slope  (rising 20 bars)",    c.sma200_slope),
        _check_row("MA Proximity  (low ≤ 1.5%)",        c.ma_proximity,   ma_hits),
        _check_row("Bullish Candle  (green + upper ½)", c.bullish_candle),
        _check_row("Volume  (≥ 20-day avg)",             c.volume_ok,      vol_ratio),
        _check_row("EMA Slope  (62 EMA rising)",         c.ema_slope_ok),
        _check_row("Clean Structure  (5-bar hold)",      c.clean_structure),
        _check_row("Not Overextended  (≤ 10% > SMA)",   c.not_overextended),
    ])

    # Use the persisted regime state that gated this scan (Issue #8).
    # This guarantees the alert shows the same regime used for gating, not a
    # potentially different freshly-calculated value.
    reg = persisted_regime or {}
    regime_line = (
        f"\n🌐 *Market Regime:*  `{reg.get('label', '—')}`  "
        f"_(SPY SMA50: ${reg.get('spy_sma50', 0):.2f} "
        f"/ SMA200: ${reg.get('spy_sma200', 0):.2f})_\n"
    ) if reg else ""

    return (
        f"🚨 *TRADE ALERT — {result.ticker}* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Entry Price:*  `${result.current_price:.2f}`\n"
        f"📊 *Signal Quality:*  {score_label}  `{c.score}/8`\n\n"
        f"📐 *Moving Averages*\n"
        f"  • 200 SMA → `${result.sma_200:.2f}`\n"
        f"  • 62  EMA → `${result.ema_62:.2f}`\n"
        f"  • 79  EMA → `${result.ema_79:.2f}`\n\n"
        f"🎯 *MA Touch:*  `{ma_hits}`\n"
        f"{regime_line}"
        f"🛡️ *Stop Loss Levels*\n"
        f"  • Hard stop  → `${s.hard_stop:.2f}`  _(79 EMA − 1× ATR)_\n"
        f"  • Primary    → `${s.primary_stop:.2f}`  _(close below 79 EMA)_\n"
        f"  • Breakdown  → `${s.catastrophic_stop:.2f}`  _(200 SMA breach)_\n"
        f"  • 🎯 Target  → `${s.partial_target:.2f}`  _(2R partial exit)_\n"
        f"  • Trail at   → `${s.trail_activation:.2f}`  _(+3% activation)_\n"
        f"  • ATR(14):  `${s.atr14:.2f}`  |  Risk: `{s.risk_pct:.2f}%`\n\n"
        f"✅ *Entry Checks  [{c.score}/8]*\n"
        f"{checks_block}\n\n"
        f"🕐 `{result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Exit thesis: daily close below 79 EMA = stop triggered._"
    )


def _build_watch_alert(result: AnalysisResult) -> str:
    c = result.checks
    ma_hits     = ", ".join(result.triggered_mas) if result.triggered_mas else "—"
    score_label = _score_label(c.score)

    failing = []
    if not c.uptrend:          failing.append("uptrend (price > 200 SMA)")
    if not c.sma200_slope:     failing.append("SMA200 slope rising (20-bar)")
    if not c.ma_proximity:     failing.append("MA proximity touch (low ≤ 1.5%)")
    if not c.bullish_candle:   failing.append("bullish candle confirmation")
    if not c.volume_ok:        failing.append("above-average volume")
    if not c.ema_slope_ok:     failing.append("62 EMA slope rising")
    if not c.clean_structure:  failing.append("clean 5-bar EMA structure")
    if not c.not_overextended: failing.append("price not overextended")

    missing_str = "\n".join(f"  ⏳ {f}" for f in failing)

    return (
        f"👀 *WATCHING — {result.ticker}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Current Price:*  `${result.current_price:.2f}`\n"
        f"📊 *Signal Quality:*  {score_label}  `{c.score}/8`\n"
        f"🎯 *MA Touch:*  `{ma_hits}`\n\n"
        f"⏳ *Still needed for full alert:*\n{missing_str}\n\n"
        f"📐 *Levels*\n"
        f"  • 200 SMA → `${result.sma_200:.2f}`\n"
        f"  • 62  EMA → `${result.ema_62:.2f}`\n"
        f"  • 79  EMA → `${result.ema_79:.2f}`\n\n"
        f"🕐 `{result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_regime_skip_message(reg_ctx: dict) -> str:
    target_labels = ", ".join(
        f"`{v}`" for k, v in REGIME_LABELS.items() if k in TARGET_REGIMES
    )
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🌐 *Daily Regime Check — Scan Skipped*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⛔ *Regime:*  `{reg_ctx.get('label', 'Unknown')}`\n"
        f"_Scans are only active in: {target_labels}_\n\n"
        f"📊 *SPY Snapshot*\n"
        f"  • Close:    `${reg_ctx.get('spy_close', 0):.2f}`\n"
        f"  • SMA50:    `${reg_ctx.get('spy_sma50', 0):.2f}`\n"
        f"  • SMA200:   `${reg_ctx.get('spy_sma200', 0):.2f}`\n"
        f"  • ATR Rank: `{reg_ctx.get('atr_rank_pct', 0):.1f}th percentile`\n\n"
        f"_No intraday scanning will occur tomorrow._\n"
        f"🕐 `{now_str}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_regime_active_message(reg_ctx: dict, ticker_count: int) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🌐 *Daily Regime Check — Scan Active*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Regime:*  `{reg_ctx.get('label', 'Unknown')}`\n"
        f"_Conditions met — {ticker_count} ticker(s) will be scanned intraday "
        f"every {INTRADAY_SCAN_INTERVAL // 60} min tomorrow._\n\n"
        f"📊 *SPY Snapshot*\n"
        f"  • Close:    `${reg_ctx.get('spy_close', 0):.2f}`\n"
        f"  • SMA50:    `${reg_ctx.get('spy_sma50', 0):.2f}`\n"
        f"  • SMA200:   `${reg_ctx.get('spy_sma200', 0):.2f}`\n"
        f"  • ATR Rank: `{reg_ctx.get('atr_rank_pct', 0):.1f}th percentile`\n\n"
        f"🕐 `{now_str}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── Cooldown helper ────────────────────────────────────────────────────────────

def _cooldown_expired(last_alert_ts_str: str | None) -> bool:
    """True if ALERT_COOLDOWN_HOURS have passed since the last alert."""
    if last_alert_ts_str is None:
        return True
    try:
        last_ts = datetime.fromisoformat(last_alert_ts_str)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_ts > timedelta(hours=ALERT_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return True


# ── Main scanner loop ──────────────────────────────────────────────────────────

async def run_scanner_loop(bot: Bot, chat_id: str) -> None:
    """
    Single heartbeat loop waking every 60 seconds.

    Each wake-up independently decides which mode to enter:
      - POST-CLOSE: if it is past 17:00 ET and has not already run today
        → run regime check, save state, optionally refresh universe.
      - INTRADAY: if it is market hours and today's saved state is a target
        → run a ticker scan, then sleep INTRADAY_SCAN_INTERVAL seconds.
      - Otherwise: sleep until next heartbeat.
    """
    logger.info(
        "Scanner loop started. IntraDay interval=%ds  Cooldown=%dh",
        INTRADAY_SCAN_INTERVAL, ALERT_COOLDOWN_HOURS,
    )

    # Tracks when the last intraday scan completed so we respect the interval.
    last_intraday_scan: datetime | None = None

    while True:
        now_utc = datetime.now(timezone.utc)

        # ── POST-CLOSE: regime check (once per calendar day) ──────────────────
        if _is_post_close() and not has_run_today():
            try:
                await _run_post_close(bot, chat_id)
            except Exception as exc:
                logger.exception("Error in post-close routine: %s", exc)

        # ── INTRADAY: ticker scan (every INTRADAY_SCAN_INTERVAL seconds) ──────
        elif _is_market_hours() and is_today_target():
            due = (
                last_intraday_scan is None
                or (now_utc - last_intraday_scan).total_seconds() >= INTRADAY_SCAN_INTERVAL
            )
            if due:
                try:
                    await _run_intraday_scan(bot, chat_id)
                except Exception as exc:
                    logger.exception("Error in intraday scan: %s", exc)
                last_intraday_scan = datetime.now(timezone.utc)

        await asyncio.sleep(_HEARTBEAT_INTERVAL)


# ── Post-close routine ─────────────────────────────────────────────────────────

async def _run_post_close(bot: Bot, chat_id: str) -> None:
    """
    Runs once per trading day after 17:00 ET.
    1. Force-refreshes the regime cache so confirmed closing SPY data is used.
    2. Fetches regime context.
    3. Saves result to regime_state.json.
    4. Sends Telegram confirmation (active or skip).
    5. If target regime: triggers automatic universe refresh.
    """
    logger.info("Post-close routine starting.")

    # Force-refresh ensures we use the confirmed daily close, not a stale
    # intraday cache built hours earlier (Issue #3).
    reg_ctx = get_regime_context(force_refresh=True)
    if not reg_ctx:
        logger.warning("Post-close: could not retrieve regime context — skipping.")
        return

    # Persist result for today
    save_regime_state(reg_ctx)

    target = bool(reg_ctx.get("is_target", False))

    if REGIME_FILTER_ENABLED and not target:
        logger.info(
            "Post-close: regime is %s — not a target. Intraday scanning suppressed tomorrow.",
            reg_ctx.get("label"),
        )
        await _send(bot, chat_id, _build_regime_skip_message(reg_ctx))
        return

    # Regime is a target — run universe refresh FIRST so the count in the
    # confirmation message reflects the freshly screened candidates (Issue #7).
    logger.info(
        "Post-close: regime is %s — target confirmed. Running universe refresh before notification.",
        reg_ctx.get("label"),
    )

    try:
        from universe import refresh_universe
        await refresh_universe(bot, chat_id)
    except Exception as exc:
        logger.exception("Post-close: universe refresh failed: %s", exc)

    # Determine ticker count AFTER refresh so the message is accurate (Issue #7).
    if UNIVERSE_MODE:
        from universe import load_candidates
        ticker_count = len(load_candidates())
    else:
        ticker_count = len(wl_store.load())

    await _send(bot, chat_id, _build_regime_active_message(reg_ctx, ticker_count))


# ── Intraday scan routine ──────────────────────────────────────────────────────

async def _run_intraday_scan(bot: Bot, chat_id: str) -> None:
    """
    Scans all tickers during market hours on days where the regime state
    saved from the previous post-close check is a target regime.
    """
    if UNIVERSE_MODE:
        from universe import load_candidates
        candidates = load_candidates()
        if not candidates:
            logger.info("Intraday scan: no universe candidates — run /refresh.")
            return
        watchlist = {t: {} for t in candidates}
    else:
        watchlist = wl_store.load()
        if not watchlist:
            logger.info("Intraday scan: watchlist is empty.")
            return

    # Load persisted regime once per scan so every alert in this cycle uses
    # the same regime context that gated the scan (Issue #8).
    state = load_regime_state()
    regime_label = state.get("label", "Unknown") if state else "Unknown"

    logger.info(
        "Intraday scan starting — %d ticker(s) | regime: %s",
        len(watchlist), regime_label,
    )

    scan_start = datetime.now(timezone.utc)

    for ticker, ticker_state in list(watchlist.items()):
        await _process_ticker(bot, chat_id, watchlist, ticker, ticker_state, state)
        await asyncio.sleep(2)

    # Issue #6 — log actual elapsed time so real-world cadence can be verified.
    elapsed = (datetime.now(timezone.utc) - scan_start).total_seconds()
    logger.info(
        "Intraday scan complete — %d ticker(s) in %.1fs "
        "(configured interval: %ds; next scan due in ~%.0fs).",
        len(watchlist), elapsed,
        INTRADAY_SCAN_INTERVAL,
        max(0, INTRADAY_SCAN_INTERVAL - elapsed),
    )


# ── Per-ticker processing ──────────────────────────────────────────────────────

async def _process_ticker(
    bot: Bot,
    chat_id: str,
    watchlist: dict,
    ticker: str,
    state: dict,
    persisted_regime: dict | None = None,
) -> None:
    """Analyse one ticker and dispatch the appropriate alert (or none)."""
    loop   = asyncio.get_running_loop()
    result: AnalysisResult = await loop.run_in_executor(
        None, fetch_and_analyse, ticker
    )

    if result.error:
        logger.warning("Skipping %s — analysis error: %s", ticker, result.error)
        return

    wl_store.update_ticker_state(
        watchlist, ticker,
        last_price = result.current_price,
        trend      = "up" if result.checks.uptrend else "down",
        last_score = result.checks.score,
    )

    cooldown_clear = _cooldown_expired(state.get("last_alert_ts"))

    # ── Path A: All 8 checks pass → full TRADE ALERT ──────────────────────────
    if result.checks.all_pass and cooldown_clear:
        await _send(bot, chat_id, _build_full_alert(result, persisted_regime))
        wl_store.update_ticker_state(
            watchlist, ticker,
            last_alert_ts=result.timestamp.isoformat(),
        )
        logger.info("FULL ALERT sent for %s  score=8/8  MAs=%s", ticker, result.triggered_mas)

    elif result.checks.all_pass and not cooldown_clear:
        logger.info("All checks pass for %s but cooldown active — suppressed.", ticker)

    # ── Path B: Score >= threshold → WATCHING notice ───────────────────────────
    elif (
        result.checks.score >= PARTIAL_ALERT_MIN_SCORE
        and result.checks.ma_proximity
        and result.checks.uptrend
        and cooldown_clear
    ):
        await _send(bot, chat_id, _build_watch_alert(result))
        logger.info("WATCH ALERT sent for %s  score=%d/8.", ticker, result.checks.score)

    else:
        logger.info(
            "%s — no alert. score=%d/8  uptrend=%s  proximity=%s",
            ticker, result.checks.score,
            result.checks.uptrend, result.checks.ma_proximity,
        )


# ── Telegram send ──────────────────────────────────────────────────────────────

async def _send(bot: Bot, chat_id: str, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
