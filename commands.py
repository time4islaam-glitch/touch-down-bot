"""
modules/commands.py — Telegram command handler functions.

Each async function is registered as a CommandHandler in bot.py.
All handlers load the watchlist from disk on demand so they always
reflect the latest state even after a bot restart.
"""

import logging
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import watchlist as wl_store
from analysis import validate_ticker

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

    text = (
        "👋 *Welcome to your Trading Alert Bot!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *Available Commands*\n\n"
        "• `/add <TICKER>` — Add a stock to the watchlist\n"
        "  _e.g._ `/add AAPL`\n\n"
        "• `/remove <TICKER>` — Remove a stock from the watchlist\n"
        "  _e.g._ `/remove AAPL`\n\n"
        "• `/watchlist` — Show all tracked tickers & status\n\n"
        "• `/help` — Show this message\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *Alert Logic*\n"
        "An alert fires when a ticker is in an *uptrend* (price > 200 SMA) "
        "*and* the current price is within 0.5 % of the 200 SMA, 62 EMA, "
        "or 79 EMA. Alerts respect a 4-hour cooldown per ticker.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Currently tracking {ticker_count} ticker(s):*\n{tickers_str}"
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
