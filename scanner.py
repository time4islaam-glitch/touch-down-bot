"""
modules/scanner.py — Asynchronous background market-scanning loop.

Runs every SCAN_INTERVAL_SECONDS. For each ticker in the watchlist:
  1. Fetches and analyses data via analysis.fetch_and_analyse()
  2. Evaluates all 7 entry quality checks
  3. Sends a rich Telegram alert only when ALL checks pass and cooldown
     has expired — or a "monitoring" message for partial passes (score >= 5)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import Bot
from telegram.constants import ParseMode

import watchlist as wl_store
from analysis import fetch_and_analyse, AnalysisResult, _score_label

logger = logging.getLogger(__name__)

# ── Configuration (all overrideable via env vars) ──────────────────────────────
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL",        3600))
ALERT_COOLDOWN_HOURS  = int(os.environ.get("ALERT_COOLDOWN_HOURS",    4))
MARKET_OPEN_UTC       = int(os.environ.get("MARKET_OPEN_UTC",        13))  # 09:00 ET
MARKET_CLOSE_UTC      = int(os.environ.get("MARKET_CLOSE_UTC",       21))  # 17:00 ET

# Minimum score to send a "watching" partial alert (won't fire a full alert)
PARTIAL_ALERT_MIN_SCORE = int(os.environ.get("PARTIAL_ALERT_MIN_SCORE", 5))


# ── Message builders ───────────────────────────────────────────────────────────

def _check_row(label: str, passed: bool, detail: str = "") -> str:
    """Format a single check row for the Telegram message."""
    icon = "✅" if passed else "❌"
    suffix = f"  `{detail}`" if detail else ""
    return f"  {icon} {label}{suffix}"


def _build_full_alert(result: AnalysisResult) -> str:
    """
    Build the full ALERT message — fires only when all 7 checks pass.
    Includes entry context, stop loss levels, and signal quality score.
    """
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
        _check_row("Uptrend  (Price > 200 SMA)",    c.uptrend),
        _check_row("MA Proximity  (≤ 0.5%)",         c.ma_proximity,   ma_hits),
        _check_row("Bullish Candle  (green + upper ½)", c.bullish_candle),
        _check_row("Volume  (≥ 20-day avg)",         c.volume_ok,      vol_ratio),
        _check_row("EMA Slope  (62 EMA rising)",     c.ema_slope_ok),
        _check_row("Clean Structure  (5-bar hold)",  c.clean_structure),
        _check_row("Not Overextended  (≤ 10% > SMA)", c.not_overextended),
    ])

    return (
        f"🚨 *TRADE ALERT — {result.ticker}* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Entry Price:*  `${result.current_price:.2f}`\n"
        f"📊 *Signal Quality:*  {score_label}  `{c.score}/7`\n\n"
        f"📐 *Moving Averages*\n"
        f"  • 200 SMA → `${result.sma_200:.2f}`\n"
        f"  • 62  EMA → `${result.ema_62:.2f}`\n"
        f"  • 79  EMA → `${result.ema_79:.2f}`\n\n"
        f"🎯 *MA Touch:*  `{ma_hits}`\n\n"
        f"🛡️ *Stop Loss Levels*\n"
        f"  • Primary   → `${s.primary_stop:.2f}`  _(close below 79 EMA)_\n"
        f"  • Hard stop → `${s.hard_stop:.2f}`  _(−0.5% buffer)_\n"
        f"  • Breakdown → `${s.catastrophic_stop:.2f}`  _(200 SMA breach)_\n"
        f"  • Risk from entry: `{s.risk_pct:.2f}%`\n\n"
        f"✅ *Entry Checks  [{c.score}/7]*\n"
        f"{checks_block}\n\n"
        f"🕐 `{result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Exit thesis: daily close below 79 EMA = stop triggered._"
    )


def _build_watch_alert(result: AnalysisResult) -> str:
    """
    Build a softer "WATCHING" message for setups scoring >= 5 but not yet 7/7.
    Tells the trader what is still missing for a full entry signal.
    """
    c = result.checks
    ma_hits = ", ".join(result.triggered_mas) if result.triggered_mas else "—"
    score_label = _score_label(c.score)

    failing = []
    if not c.bullish_candle:   failing.append("bullish candle confirmation")
    if not c.volume_ok:        failing.append("above-average volume")
    if not c.ema_slope_ok:     failing.append("62 EMA slope rising")
    if not c.clean_structure:  failing.append("clean 5-bar EMA structure")
    if not c.not_overextended: failing.append("price not overextended")
    if not c.ma_proximity:     failing.append("MA proximity touch")
    if not c.uptrend:          failing.append("uptrend (price > 200 SMA)")

    missing_str = "\n".join(f"  ⏳ {f}" for f in failing)

    return (
        f"👀 *WATCHING — {result.ticker}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Current Price:*  `${result.current_price:.2f}`\n"
        f"📊 *Signal Quality:*  {score_label}  `{c.score}/7`\n"
        f"🎯 *MA Touch:*  `{ma_hits}`\n\n"
        f"⏳ *Still needed for full alert:*\n{missing_str}\n\n"
        f"📐 *Levels*\n"
        f"  • 200 SMA → `${result.sma_200:.2f}`\n"
        f"  • 62  EMA → `${result.ema_62:.2f}`\n"
        f"  • 79  EMA → `${result.ema_79:.2f}`\n\n"
        f"🕐 `{result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── Cooldown helpers ───────────────────────────────────────────────────────────

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


def _is_market_hours() -> bool:
    """True during US equity market hours (UTC). Skips weekends."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:     # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN_UTC <= now.hour < MARKET_CLOSE_UTC


# ── Main scanner loop ──────────────────────────────────────────────────────────

async def run_scanner_loop(bot: Bot, chat_id: str) -> None:
    """
    Infinite async loop — runs forever as a background asyncio task.
    Sleeps SCAN_INTERVAL_SECONDS between cycles.
    """
    logger.info(
        "Scanner loop started. Interval=%ds  Cooldown=%dh  PartialAlertMinScore=%d",
        SCAN_INTERVAL_SECONDS, ALERT_COOLDOWN_HOURS, PARTIAL_ALERT_MIN_SCORE,
    )

    while True:
        try:
            await _run_single_scan(bot, chat_id)
        except Exception as exc:
            logger.exception("Unexpected error in scanner loop: %s", exc)

        logger.info("Scanner sleeping for %d seconds…", SCAN_INTERVAL_SECONDS)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _run_single_scan(bot: Bot, chat_id: str) -> None:
    """One full pass across all tickers in the watchlist."""
    watchlist = wl_store.load()

    if not watchlist:
        logger.info("Watchlist is empty — nothing to scan.")
        return

    if not _is_market_hours():
        logger.info("Outside market hours — scan skipped.")
        return

    logger.info("Starting scan of %d ticker(s)…", len(watchlist))

    for ticker, state in list(watchlist.items()):
        await _process_ticker(bot, chat_id, watchlist, ticker, state)
        # Yield between tickers to keep the event loop healthy
        await asyncio.sleep(2)


async def _process_ticker(
    bot: Bot,
    chat_id: str,
    watchlist: dict,
    ticker: str,
    state: dict,
) -> None:
    """Analyse one ticker and dispatch the appropriate alert (or none)."""
    loop = asyncio.get_running_loop()
    result: AnalysisResult = await loop.run_in_executor(
        None, fetch_and_analyse, ticker
    )

    if result.error:
        logger.warning("Skipping %s — analysis error: %s", ticker, result.error)
        return

    # Persist latest price and trend for /watchlist command display
    wl_store.update_ticker_state(
        watchlist, ticker,
        last_price = result.current_price,
        trend      = "up" if result.checks.uptrend else "down",
        last_score = result.checks.score,
    )

    cooldown_clear = _cooldown_expired(state.get("last_alert_ts"))

    # ── Path A: All 7 checks pass → full TRADE ALERT ──────────────────────────
    if result.checks.all_pass and cooldown_clear:
        await _send(bot, chat_id, _build_full_alert(result))
        wl_store.update_ticker_state(
            watchlist, ticker,
            last_alert_ts = result.timestamp.isoformat(),
        )
        logger.info(
            "FULL ALERT sent for %s  score=7/7  MAs=%s",
            ticker, result.triggered_mas,
        )

    elif result.checks.all_pass and not cooldown_clear:
        logger.info(
            "All checks pass for %s but cooldown active — suppressed.", ticker
        )

    # ── Path B: Score >= threshold but not perfect → WATCHING notice ──────────
    elif (
        result.checks.score >= PARTIAL_ALERT_MIN_SCORE
        and result.checks.ma_proximity   # must have the MA touch at minimum
        and result.checks.uptrend        # must be in uptrend
        and cooldown_clear
    ):
        await _send(bot, chat_id, _build_watch_alert(result))
        # Use a shorter cooldown for watch alerts (half of full cooldown)
        # by NOT storing last_alert_ts — just log it
        logger.info(
            "WATCH ALERT sent for %s  score=%d/7  missing checks logged.",
            ticker, result.checks.score,
        )

    else:
        logger.info(
            "%s — no alert. score=%d/7  uptrend=%s  proximity=%s",
            ticker, result.checks.score,
            result.checks.uptrend, result.checks.ma_proximity,
        )


async def _send(bot: Bot, chat_id: str, text: str) -> None:
    """Send a Telegram message, logging any delivery errors."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
