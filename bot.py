"""
bot.py — Entry point for the Railway-deployed Telegram Trading Bot.

Starts the Telegram application, registers command handlers,
and launches the background market-scanning loop.
"""

import asyncio
import logging
import os

from telegram.ext import Application, CommandHandler

from modules.commands import (
    cmd_start,
    cmd_add,
    cmd_remove,
    cmd_watchlist,
)
from modules.scanner import run_scanner_loop
from modules.trading212 import Trading212Client

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Required env vars ─────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


async def post_init(application: Application) -> None:
    """
    Runs once after the bot is fully initialised.
    - Verifies Trading 212 connectivity.
    - Sends a startup message to the configured chat.
    - Schedules the background scanner loop.
    """
    # ── Trading 212 health-check ──────────────────────────────────────────────
    t212 = Trading212Client()
    if t212.enabled:
        try:
            account_info = t212.get_account_metadata()
            logger.info("Trading212 connected — account: %s", account_info)
        except Exception as exc:
            logger.warning("Trading212 check failed (non-fatal): %s", exc)
    else:
        logger.info("Trading212 integration disabled (no API key configured).")

    # ── Startup Telegram notification ─────────────────────────────────────────
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🤖 *Trading Bot is online!*\n\n"
            "Use /help to see available commands.\n"
            "Market scanner will run every hour during market hours."
        ),
        parse_mode="Markdown",
    )

    # ── Launch background scanner as an asyncio task ──────────────────────────
    asyncio.create_task(
        run_scanner_loop(application.bot, TELEGRAM_CHAT_ID),
        name="market_scanner",
    )
    logger.info("Background scanner task scheduled.")


def main() -> None:
    """Build the Application, register handlers and start polling."""
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_start))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))

    logger.info("Bot starting — polling for updates…")
    # run_polling blocks forever; drop_pending_updates avoids replaying old cmds
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
