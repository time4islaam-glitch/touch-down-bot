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

    # Basic sanity check on ticker format
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

    # Validate ticker via a quick yfinance call
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
        f"It will be included in the next scheduled scan.",
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

    # Basic format validation
    invalid_format = [t for t in tickers if not t.isalpha() or len(t) > 10]
    if invalid_format:
        await update.message.reply_text(
            "⚠️ Invalid ticker(s): " + ", ".join(f"`{t}`" for t in invalid_format),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    watchlist = wl_store.load()
    already = [t for t in tickers if t in watchlist]
    to_add = [t for t in tickers if t not in watchlist]

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

    ticker = context.args[0].upper().strip()
    watchlist = wl_store.load()

    removed = wl_store.remove_ticker(watchlist, ticker)

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
        state = watchlist[ticker]
        price     = state.get("last_price")
        trend     = state.get("trend")
        last_alert = state.get("last_alert_ts")

        price_str = f"${price:.2f}" if price else "—"
        trend_str = ("📈 Up" if trend == "up" else "📉 Down") if trend else "❓ Unknown"

        if last_alert:
            try:
                ts = datetime.fromisoformat(last_alert)
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


# ── /refresh ──────────────────────────────────────────────────────────────────

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /refresh — manually trigger a universe refresh.
    Admin-only: only responds if the caller's Telegram user ID matches
    the ADMIN_USER_ID env var.
    """
    admin_id = int(os.environ.get("ADMIN_USER_ID", "0"))
    caller_id = update.effective_user.id if update.effective_user else 0

    if admin_id == 0 or caller_id != admin_id:
        await update.message.reply_text(
            "⛔ This command is restricted to the bot administrator.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "🔄 Starting universe refresh… this may take a few minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )

    import asyncio
    from universe import refresh_universe

    try:
        count = await refresh_universe(
            update.get_bot(),
            str(update.effective_chat.id),
        )
        await update.message.reply_text(
            f"✅ Universe refresh complete — `{count}` candidates saved.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.exception("cmd_refresh error: %s", exc)
        await update.message.reply_text(
            f"❌ Universe refresh failed: `{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /universe ─────────────────────────────────────────────────────────────────

async def cmd_universe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /universe — show current universe mode status, candidate count, and last refresh time.
    """
    from universe import load_candidates, get_last_refresh_time, ENABLED_EXCHANGES
    from scanner import UNIVERSE_MODE

    candidates   = load_candidates()
    last_refresh = get_last_refresh_time()
    mode_str     = "✅ *ON*" if UNIVERSE_MODE else "⛔ *OFF*"
    exchanges    = ", ".join(ENABLED_EXCHANGES)

    if last_refresh:
        try:
            ts = datetime.fromisoformat(last_refresh)
            refresh_str = ts.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            refresh_str = last_refresh
    else:
        refresh_str = "Never"

    text = (
        f"🌐 *Universe Mode Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: {mode_str}\n"
        f"📋 Exchanges: `{exchanges}`\n"
        f"🔍 Candidates loaded: `{len(candidates)}`\n"
        f"🕐 Last refresh: `{refresh_str}`\n\n"
        f"_Use /refresh to manually trigger a new universe scan._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
