"""
regime_state.py — Persists the daily post-close regime gate result to disk.

Reads and writes regime_state.json so the result of the post-close regime
check survives Railway restarts and drives the following day's intraday scans.

JSON structure:
{
    "date":         "2026-06-05",        # US/Eastern calendar date of the close
    "regime":       "CORRECTION",
    "label":        "Correction / Dip",
    "is_target":    true,
    "spy_close":    523.41,
    "spy_sma50":    519.20,
    "spy_sma200":   498.33,
    "atr_rank_pct": 72.1,
    "checked_at":   "2026-06-05T21:35:00+00:00"
}

Public interface:
    save_regime_state(reg_ctx: dict) -> None
    load_regime_state() -> dict | None
    is_today_target() -> bool        # True if the most recent saved state is a target regime
    has_run_today() -> bool          # True if post-close check already ran this calendar day

Session-aware design (fixes Issues #1, #2, #5):
    The saved regime state represents the MOST RECENT completed trading session.
    It remains valid — and drives intraday scanning — until the NEXT post-close
    check replaces it.  Calendar-date equality is NOT required.

    Examples of correct behaviour:
      • Monday close saves state → Tuesday intraday scanning uses it  ✅
      • Friday close saves state → Monday intraday scanning uses it   ✅
      • Holiday gap             → state from last session still valid ✅

    has_run_today() still uses a strict calendar-date check so the post-close
    routine fires exactly once per calendar day (not once per session gap).
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytz

logger = logging.getLogger(__name__)

REGIME_STATE_PATH = Path(os.environ.get("REGIME_STATE_PATH", "regime_state.json"))
EASTERN = pytz.timezone("US/Eastern")


def _today_eastern() -> str:
    """Return today's date string in US/Eastern time (YYYY-MM-DD)."""
    return datetime.now(EASTERN).strftime("%Y-%m-%d")


def save_regime_state(reg_ctx: dict) -> None:
    """
    Persist the regime gate result for today's close.
    reg_ctx is the dict returned by regime.get_regime_context().
    """
    payload = {
        "date":         _today_eastern(),
        "regime":       reg_ctx.get("regime", "UNKNOWN"),
        "label":        reg_ctx.get("label", "Unknown"),
        "is_target":    bool(reg_ctx.get("is_target", False)),
        "spy_close":    reg_ctx.get("spy_close", 0.0),
        "spy_sma50":    reg_ctx.get("spy_sma50", 0.0),
        "spy_sma200":   reg_ctx.get("spy_sma200", 0.0),
        "atr_rank_pct": reg_ctx.get("atr_rank_pct", 0.0),
        "checked_at":   datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = REGIME_STATE_PATH.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(REGIME_STATE_PATH)
        logger.info(
            "Regime state saved: %s | is_target=%s | date=%s",
            payload["label"], payload["is_target"], payload["date"],
        )
    except OSError as exc:
        logger.error("Failed to save regime state: %s", exc)


def load_regime_state() -> dict | None:
    """
    Load the persisted regime state from disk.
    Returns None if the file is missing or unreadable.
    """
    if not REGIME_STATE_PATH.exists():
        return None
    try:
        with REGIME_STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load regime state: %s", exc)
        return None


def is_today_target() -> bool:
    """
    Returns True if the most recent saved regime state is a target regime.

    Intentionally does NOT require the saved date to equal today's calendar
    date.  The persisted state represents the most recently completed trading
    session and remains valid — and drives intraday scanning — until the next
    post-close check overwrites it.  This ensures Friday → Monday and any
    holiday-gap transitions work correctly (Issues #1, #2, #5).

    A state whose is_target field is missing or non-boolean is treated as
    corrupt and returns False (Issue #10 partial mitigation).
    """
    state = load_regime_state()
    if state is None:
        return False
    # Sanity-check the required fields to catch partial writes (Issue #10)
    if "is_target" not in state or "regime" not in state:
        logger.warning(
            "Regime state file appears corrupt (missing required fields) — "
            "treating as non-target to suppress scanning."
        )
        return False
    return bool(state.get("is_target", False))


def has_run_today() -> bool:
    """
    Returns True if the post-close regime check has already run today
    (US/Eastern calendar day).  Uses a strict date match so the post-close
    routine fires at most once per calendar day, regardless of session gaps.
    """
    state = load_regime_state()
    if state is None:
        return False
    if "date" not in state:
        # Corrupt file — allow post-close to re-run and overwrite
        logger.warning("Regime state missing 'date' field — allowing post-close re-run.")
        return False
    return state.get("date") == _today_eastern()
