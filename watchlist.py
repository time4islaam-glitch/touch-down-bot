"""
modules/watchlist.py — JSON-backed persistent watchlist.

Reads and writes watchlist.json so the ticker list survives Railway
restarts, redeployments, or container sleeps.

Thread/async safety note: all mutations are synchronous dict operations
followed by an atomic file write; the GIL ensures correctness for our
single-threaded asyncio model.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Location of the JSON store — Railway mounts a writable filesystem at /app
WATCHLIST_PATH = Path(os.environ.get("WATCHLIST_PATH", "watchlist.json"))


def _default_entry() -> Dict[str, Any]:
    """Return a fresh watchlist entry for a new ticker."""
    return {
        "last_alert_ts": None,   # ISO timestamp of the last alert sent
        "last_price":    None,   # Last known close price
        "trend":         None,   # "up" | "down" | None
    }


def load() -> Dict[str, Any]:
    """
    Load the watchlist from disk.
    Returns an empty dict if the file doesn't exist yet.
    """
    if not WATCHLIST_PATH.exists():
        logger.info("No watchlist file found — starting with empty watchlist.")
        return {}
    try:
        with WATCHLIST_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        logger.info("Watchlist loaded: %d ticker(s).", len(data))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load watchlist: %s. Starting fresh.", exc)
        return {}


def save(watchlist: Dict[str, Any]) -> None:
    """Persist the watchlist dict to disk atomically via a temp file."""
    tmp_path = WATCHLIST_PATH.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(watchlist, fh, indent=2, default=str)
        tmp_path.replace(WATCHLIST_PATH)
        logger.debug("Watchlist saved (%d tickers).", len(watchlist))
    except OSError as exc:
        logger.error("Failed to save watchlist: %s", exc)


def add_ticker(watchlist: Dict[str, Any], ticker: str) -> bool:
    """
    Add *ticker* (uppercased) to the watchlist.
    Returns True if it was newly added, False if already present.
    """
    ticker = ticker.upper()
    if ticker in watchlist:
        return False
    watchlist[ticker] = _default_entry()
    save(watchlist)
    return True


def remove_ticker(watchlist: Dict[str, Any], ticker: str) -> bool:
    """
    Remove *ticker* from the watchlist.
    Returns True if removed, False if it wasn't tracked.
    """
    ticker = ticker.upper()
    if ticker not in watchlist:
        return False
    del watchlist[ticker]
    save(watchlist)
    return True


def update_ticker_state(
    watchlist: Dict[str, Any],
    ticker: str,
    **kwargs: Any,
) -> None:
    """
    Merge **kwargs into the ticker's state dict and persist.
    Typical usage: update_ticker_state(wl, "AAPL", last_alert_ts="2024-…")
    """
    ticker = ticker.upper()
    if ticker in watchlist:
        watchlist[ticker].update(kwargs)
        save(watchlist)
