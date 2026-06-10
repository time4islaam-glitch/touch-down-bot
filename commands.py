"""
modules/commands.py — Telegram command handler functions.

Each async function is registered as a CommandHandler in bot.py.
All handlers load the watchlist from disk on demand so they always
reflect the latest state even after a bot restart.
"""

import logging
import os
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import watchlist as wl_store
from analysis import validate_ticker
from regime import get_regime_context, TARGET_REGIMES, REGIME_LABELS
from regime_state import load_state, save_weekly, save_daily, is_market_active, is_day_active

logger = logging.getLogger(__name__)


# ── /start  &  /help ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a help / status overview message."""
    watchlist = wl_store.load()
    ticker_count = len(watchlist)
    tickers_str = (
        ", ".join(f"`{t}`" for t in sorted(watchlist))
        if watchlist
        else "_none yet_"
    )

    regime_ctx = get_regime_context()
    regime_status = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 *Current Market Regime*\n"
        f"`{regime_ctx.get('label', 'Unknown')}`  "
        f"{'✅ Active — signals enabled' if regime_ctx.get('is_target') else '⛔ Inactive — signals suppressed'}\n"
        f"_SPY: ${regime_ctx.get('spy_close', 0):.2f}_"
    ) if regime_ctx else ""

    text = (
        "👋 *Welcome to your Trading Alert Bot!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *Available Commands*\n\n"
        "• `/add <TICKER>` — Add a stock to the watchlist\n"
        "  _e.g._ `/add AAPL`\n\n"
        "• `/addmany <TICKER> ...` — Add multiple stocks at once\n"
        "  _e.g._ `/addmany AAPL TSLA NVDA MSFT`\n\n"
        "• `/remove <TICKER>` — Remove a stock from the watchlist\n"
        "  _e.g._ `/remove AAPL`\n\n"
        "• `/watchlist` — Show all tracked tickers & status\n\n"
        "• `/universe` — Show universe mode status & last regime state\n\n"
        "• `/refresh` — Manually trigger a regime re-check _(admin only)_\n\n"
        "• `/refreshuniverse` — Rescan exchanges & rebuild candidate list _(admin only)_\n\n"
        "• `/scannow` — Force an immediate Tier 3 ticker scan _(admin only)_\n\n"
        "• `/help` — Show this message\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *Alert Logic*\n"
        "An alert fires when all 8 entry checks pass: uptrend, SMA200 slope, "
        "MA proximity (low within 1.5% of 62 EMA / 79 EMA / 200 SMA), "
        "bullish candle, volume, EMA slope, clean structure, and not overextended. "
        "Alerts respect a 4-hour cooldown per ticker.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Currently tracking {ticker_count} ticker(s):*\n{tickers_str}"
        f"{regime_status}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /add ──────────────────────────────────────────────────────────────────────

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add a ticker to the watchlist.
    Usage: /add TICKER
    """
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a ticker symbol.\n_Example:_ `/add AAPL`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    ticker = context.args[0].upper().strip()

    if not ticker.isalpha() or len(ticker) > 10:
        await update.message.reply_text(
            f"⚠️ `{ticker}` doesn't look like a valid ticker symbol.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    watchlist = wl_store.load()

    if ticker in watchlist:
        await update.message.reply_text(
            f"ℹ️ `{ticker}` is already on the watchlist.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"🔍 Validating `{ticker}`… please wait.",
        parse_mode=ParseMode.MARKDOWN,
    )

    import asyncio
    loop = asyncio.get_running_loop()
    is_valid = await loop.run_in_executor(None, validate_ticker, ticker)

    if not is_valid:
        await update.message.reply_text(
            f"❌ Could not fetch data for `{ticker}`. "
            "Please check the symbol and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    wl_store.add_ticker(watchlist, ticker)
    await update.message.reply_text(
        f"✅ `{ticker}` has been added to the watchlist!\n"
        f"It will be included in the next intraday scan.",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("Ticker %s added by user %s", ticker, update.effective_user.id)


# ── /addmany ──────────────────────────────────────────────────────────────────

async def cmd_addmany(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add multiple tickers to the watchlist in one command.
    Usage: /addmany AAPL TSLA NVDA MSFT
    """
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide at least one ticker symbol.\n"
            "_Example:_ `/addmany AAPL TSLA NVDA`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    import asyncio

    tickers = [t.upper().strip() for t in context.args]

    invalid_format = [t for t in tickers if not t.isalpha() or len(t) > 10]
    if invalid_format:
        await update.message.reply_text(
            "⚠️ Invalid ticker(s): " + ", ".join(f"`{t}`" for t in invalid_format),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    watchlist = wl_store.load()
    already = [t for t in tickers if t in watchlist]
    to_add  = [t for t in tickers if t not in watchlist]

    if not to_add:
        await update.message.reply_text(
            "ℹ️ All provided tickers are already on the watchlist.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"🔍 Validating {len(to_add)} ticker(s)… please wait.",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, validate_ticker, t) for t in to_add]
    )

    added, failed = [], []
    for ticker, valid in zip(to_add, results):
        if valid:
            wl_store.add_ticker(watchlist, ticker)
            added.append(ticker)
        else:
            failed.append(ticker)

    lines = []
    if added:
        lines.append("✅ *Added:* " + ", ".join(f"`{t}`" for t in added))
    if already:
        lines.append("ℹ️ *Already tracked:* " + ", ".join(f"`{t}`" for t in already))
    if failed:
        lines.append("❌ *Not found:* " + ", ".join(f"`{t}`" for t in failed))

    lines.append(f"\n_Watchlist now has {len(wl_store.load())} ticker(s)._")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    logger.info(
        "addmany by user %s — added: %s, failed: %s",
        update.effective_user.id, added, failed,
    )


# ── /remove ───────────────────────────────────────────────────────────────────

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Remove a ticker from the watchlist.
    Usage: /remove TICKER
    """
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a ticker symbol.\n_Example:_ `/remove AAPL`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    ticker    = context.args[0].upper().strip()
    watchlist = wl_store.load()
    removed   = wl_store.remove_ticker(watchlist, ticker)

    if removed:
        await update.message.reply_text(
            f"🗑️ `{ticker}` has been removed from the watchlist.",
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info("Ticker %s removed by user %s", ticker, update.effective_user.id)
    else:
        await update.message.reply_text(
            f"ℹ️ `{ticker}` was not on the watchlist.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /watchlist ────────────────────────────────────────────────────────────────

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display all tracked tickers with their last known state."""
    watchlist = wl_store.load()

    if not watchlist:
        await update.message.reply_text(
            "📋 Your watchlist is empty.\nUse `/add TICKER` to start tracking.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = ["📋 *Current Watchlist*\n━━━━━━━━━━━━━━━━━━━━━━"]

    for ticker in sorted(watchlist):
        state      = watchlist[ticker]
        price      = state.get("last_price")
        trend      = state.get("trend")
        last_alert = state.get("last_alert_ts")

        price_str = f"${price:.2f}" if price else "—"
        trend_str = ("📈 Up" if trend == "up" else "📉 Down") if trend else "❓ Unknown"

        if last_alert:
            try:
                ts        = datetime.fromisoformat(last_alert)
                alert_str = ts.strftime("%m/%d %H:%M UTC")
            except ValueError:
                alert_str = last_alert
        else:
            alert_str = "Never"

        lines.append(
            f"\n*{ticker}*\n"
            f"  💰 Price: `{price_str}`\n"
            f"  {trend_str}\n"
            f"  🔔 Last alert: `{alert_str}`"
        )

    lines.append(
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Total: {len(watchlist)} ticker(s)_"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /status — Show the current three-tier regime gate state at a glance.
    Tells the user exactly why the bot is active or silent right now.
    """
    state  = load_state()
    weekly = state.get("weekly", {})
    daily  = state.get("daily",  {})

    # Weekly gate
    if weekly:
        w_regime  = weekly.get("label", "Unknown")
        w_active  = weekly.get("market_active", False)
        w_week    = weekly.get("week_of", "—")
        w_checked = weekly.get("checked_at", "")
        try:
            w_ts = datetime.fromisoformat(w_checked).strftime("%a %d %b %H:%M UTC")
        except Exception:
            w_ts = w_checked
        w_icon = "✅" if w_active else "⛔"
        w_line = (
            f"{w_icon} *Weekly gate:* `{w_regime}`\n"
            f"  Week of `{w_week}` · checked `{w_ts}`\n"
            f"  {'Scanning active this week' if w_active else 'Bot silent this week'}"
        )
    else:
        w_line = "⚠️ *Weekly gate:* not yet run — use /refresh to trigger"

    # Daily gate
    if daily and weekly.get("market_active"):
        d_regime  = daily.get("label", "Unknown")
        d_active  = daily.get("day_active", False)
        d_date    = daily.get("date", "—")
        d_icon    = "✅" if d_active else "⏭️"
        d_line = (
            f"{d_icon} *Daily gate:* `{d_regime}`\n"
            f"  `{d_date}` · "
            f"{'Scanning today' if d_active else 'No scan today — not a target regime'}"
        )
    elif not weekly.get("market_active"):
        d_line = "⛔ *Daily gate:* not checked — market inactive"
    else:
        d_line = "⏳ *Daily gate:* pending pre-market check today"

    # Overall status
    if is_day_active():
        overall = "🟢 *Bot is ACTIVE — scanning universe during market hours*"
    elif is_market_active():
        overall = "🟡 *Bot is STANDBY — weekly gate open, daily gate closed*"
    else:
        overall = "🔴 *Bot is SILENT — weekly gate closed (non-bull regime)*"

    # Target regimes reminder
    target_str = " / ".join(
        REGIME_LABELS[r] for r in sorted(TARGET_REGIMES) if r in REGIME_LABELS
    )

    text = (
        f"📡 *Regime Gate Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{overall}\n\n"
        f"{w_line}\n\n"
        f"{d_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Target regimes: {target_str}_\n"
        f"_Weekly check: Fridays post-close_\n"
        f"_Daily check: weekdays 07:00 ET pre-market_\n"
        f"_Use /refresh to force a regime re-check now_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /refresh ──────────────────────────────────────────────────────────────────

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /refresh — Manually force a regime re-check right now.
    Admin-only. Updates both weekly and daily gates regardless of schedule.
    Use when market conditions have changed materially mid-week.
    """
    admin_id  = int(os.environ.get("ADMIN_USER_ID", "0"))
    caller_id = update.effective_user.id if update.effective_user else 0

    if admin_id == 0 or caller_id != admin_id:
        await update.message.reply_text(
            "⛔ /refresh is restricted to the bot administrator.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "🔄 Running forced regime check…", parse_mode=ParseMode.MARKDOWN
    )

    try:
        ctx = get_regime_context()
        if not ctx:
            await update.message.reply_text(
                "❌ Could not fetch regime data. Try again shortly.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Write both tiers so the bot is immediately in a consistent state
        save_weekly(ctx)
        save_daily(ctx)

        regime  = ctx.get("label", "Unknown")
        bull    = ctx.get("regime") in {"BULL_TREND", "BULL_HIGH_VOL", "CORRECTION"}
        target  = ctx.get("is_target", False)
        close   = ctx.get("spy_close", 0)

        status_icon = "✅" if target else "🟡" if bull else "⛔"
        status_text = (
            "Target regime — daily scans will run"
            if target else
            "Bull regime — daily gate will re-check each morning"
            if bull else
            "Non-bull regime — bot silent until next check"
        )

        await update.message.reply_text(
            f"✅ *Regime re-check complete*\n\n"
            f"{status_icon} `{regime}`  (SPY ${close:.2f})\n"
            f"_{status_text}_",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:
        logger.exception("cmd_refresh error: %s", exc)
        await update.message.reply_text(
            f"❌ Refresh failed: `{exc}`", parse_mode=ParseMode.MARKDOWN
        )


# ── /universe ─────────────────────────────────────────────────────────────────

async def cmd_universe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /universe — show universe mode status, candidate count, last refresh time,
    and the saved regime state from the most recent post-close check.
    """
    from universe import load_candidates, get_last_refresh_time, ENABLED_EXCHANGES
    from scanner import UNIVERSE_MODE, SCAN_INTERVAL_SECONDS as INTRADAY_SCAN_INTERVAL

    candidates   = load_candidates()
    last_refresh = get_last_refresh_time()
    mode_str     = "✅ *ON*" if UNIVERSE_MODE else "⛔ *OFF*"
    exchanges    = ", ".join(ENABLED_EXCHANGES)

    if last_refresh:
        try:
            ts          = datetime.fromisoformat(last_refresh)
            refresh_str = ts.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            refresh_str = last_refresh
    else:
        refresh_str = "Never"

    # Regime state from last post-close check
    regime_state = load_state()
    if regime_state:
        rs_date   = regime_state.get("date", "—")
        rs_label  = regime_state.get("label", "Unknown")
        rs_target = "✅ Active" if regime_state.get("is_target") else "⛔ Inactive"
        rs_str = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 *Last Post-Close Regime Check*\n"
            f"  • Date:   `{rs_date}`\n"
            f"  • Regime: `{rs_label}`\n"
            f"  • Status: {rs_target}\n"
            f"  • Intraday scanning: "
            f"{'every ' + str(INTRADAY_SCAN_INTERVAL // 60) + ' min' if regime_state.get('is_target') else 'suppressed'}"
        )
    else:
        rs_str = "\n\n_No post-close regime check has run yet._"

    text = (
        f"🌐 *Universe Mode Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: {mode_str}\n"
        f"📋 Exchanges: `{exchanges}`\n"
        f"🔍 Candidates loaded: `{len(candidates)}`\n"
        f"🕐 Last refresh: `{refresh_str}`"
        f"{rs_str}\n\n"
        f"_Use /refreshuniverse to rescan exchanges for candidates._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /refreshuniverse ──────────────────────────────────────────────────────────

async def cmd_refreshuniverse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /refreshuniverse — Manually rescan all enabled exchanges (NASDAQ/NYSE/LSE),
    apply pre-screen filters, and rebuild candidates.json.
    Admin-only. This is the full universe ticker scan, separate from /refresh
    (which only re-runs the regime gate check).
    """
    from universe import refresh_universe, ENABLED_EXCHANGES

    admin_id  = int(os.environ.get("ADMIN_USER_ID", "0"))
    caller_id = update.effective_user.id if update.effective_user else 0

    if admin_id == 0 or caller_id != admin_id:
        await update.message.reply_text(
            "\u26d4 /refreshuniverse is restricted to the bot administrator.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"\U0001f504 Refreshing universe \u2014 scanning `{', '.join(ENABLED_EXCHANGES)}`\u2026 "
        f"this may take a few minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        count = await refresh_universe(context.bot, str(update.effective_chat.id))
        logger.info("Universe refresh complete via /refreshuniverse \u2014 %d candidates", count)
    except Exception as exc:
        logger.exception("cmd_refreshuniverse error: %s", exc)
        await update.message.reply_text(
            f"\u274c Universe refresh failed: `{exc}`", parse_mode=ParseMode.MARKDOWN
        )


# ── /scannow ───────────────────────────────────────────────────────────────────

async def cmd_scannow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /scannow — Manually trigger a Tier 3 ticker scan immediately.
    Admin-only. Bypasses market-hours, day_active, and the SCAN_INTERVAL
    cooldown entirely — runs the same two-pass scan/alert logic as the
    regular intraday scanner, on demand.
    """
    from scanner import _run_ticker_scan

    admin_id  = int(os.environ.get("ADMIN_USER_ID", "0"))
    caller_id = update.effective_user.id if update.effective_user else 0

    if admin_id == 0 or caller_id != admin_id:
        await update.message.reply_text(
            "⛔ /scannow is restricted to the bot administrator.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "🔄 Running ticker scan now — this may take a while…",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        await _run_ticker_scan(context.bot, str(update.effective_chat.id))
        logger.info("Manual scan complete via /scannow")
    except Exception as exc:
        logger.exception("cmd_scannow error: %s", exc)
        await update.message.reply_text(
            f"❌ Scan failed: `{exc}`", parse_mode=ParseMode.MARKDOWN
        )
