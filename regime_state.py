"""
regime_state.py — Three-tier regime gate persistence.
======================================================
Stores two independent regime checks on disk so state survives
Railway restarts between checks.

TIER 1 — Weekly gate  (runs Friday post-close, ~17:00 ET)
  Answers: is the broad market in a bull environment at all?
  MARKET_ACTIVE = True  when regime is BULL_TREND, BULL_HIGH_VOL, or CORRECTION
  MARKET_ACTIVE = False when regime is BEAR or CONSOLIDATION
  If MARKET_ACTIVE is False the bot is completely silent for the week.

TIER 2 — Daily gate  (runs each weekday pre-market, ~07:00 ET)
  Answers: is today specifically a scannable day?
  DAY_ACTIVE = True  when daily regime is CORRECTION or BULL_HIGH_VOL
  DAY_ACTIVE = False when daily regime is BULL_TREND (or anything else)
  If DAY_ACTIVE is False no ticker scans run today.

TIER 3 — Intraday scan  (existing scanner loop, unchanged)
  Runs every SCAN_INTERVAL_SECONDS during market hours.
  Only executes when both MARKET_ACTIVE and DAY_ACTIVE are True.

State file structure (regime_state.json):
{
  "weekly": {
    "week_of":        "2026-06-01",    # Monday of the checked week
    "checked_at":     "2026-06-05T21:35:00+00:00",
    "regime":         "CORRECTION",
    "label":          "Correction / Dip",
    "market_active":  true,
    "spy_close":      523.41,
    "spy_sma50":      519.20,
    "spy_sma200":     498.33,
    "atr_rank_pct":   72.1
  },
  "daily": {
    "date":           "2026-06-06",
    "checked_at":     "2026-06-06T11:00:00+00:00",
    "regime":         "CORRECTION",
    "label":          "Correction / Dip",
    "day_active":     true
  }
}

Public interface:
    save_weekly(reg_ctx)      -> None
    save_daily(reg_ctx)       -> None
    load_state()              -> dict
    is_market_active()        -> bool   # weekly gate
    is_day_active()           -> bool   # daily gate
    has_weekly_run_this_week()-> bool
    has_daily_run_today()     -> bool
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytz

logger = logging.getLogger(__name__)

STATE_PATH = Path(os.environ.get("REGIME_STATE_PATH", "regime_state.json"))
EASTERN    = pytz.timezone("US/Eastern")

# Regimes where the weekly gate opens (market is in bull territory)
BULL_REGIMES    = {"BULL_TREND", "BULL_HIGH_VOL", "CORRECTION"}
# Regimes where the daily gate opens (scannable today)
TARGET_REGIMES  = {"CORRECTION", "BULL_HIGH_VOL"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(EASTERN)

def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")

def _week_of_et() -> str:
    """Monday of the current week in ET, as YYYY-MM-DD."""
    now = _now_et()
    monday = now - __import__("datetime").timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")


def _load_raw() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read regime state: %s", exc)
        return {}


def _save_raw(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.error("Failed to save regime state: %s", exc)


# ── Writers ────────────────────────────────────────────────────────────────────

def save_weekly(reg_ctx: dict) -> None:
    """
    Persist the weekly gate result after Friday's close.
    reg_ctx = dict from regime.get_regime_context()
    """
    regime        = reg_ctx.get("regime", "UNKNOWN")
    market_active = regime in BULL_REGIMES

    state = _load_raw()
    state["weekly"] = {
        "week_of":       _week_of_et(),
        "checked_at":    datetime.now(timezone.utc).isoformat(),
        "regime":        regime,
        "label":         reg_ctx.get("label", "Unknown"),
        "market_active": market_active,
        "spy_close":     reg_ctx.get("spy_close",    0.0),
        "spy_sma50":     reg_ctx.get("spy_sma50",    0.0),
        "spy_sma200":    reg_ctx.get("spy_sma200",   0.0),
        "atr_rank_pct":  reg_ctx.get("atr_rank_pct", 0.0),
    }
    _save_raw(state)
    logger.info(
        "Weekly regime saved: %s | market_active=%s | week_of=%s",
        regime, market_active, state["weekly"]["week_of"],
    )


def save_daily(reg_ctx: dict) -> None:
    """
    Persist the daily gate result after the pre-market check.
    reg_ctx = dict from regime.get_regime_context()
    """
    regime     = reg_ctx.get("regime", "UNKNOWN")
    day_active = regime in TARGET_REGIMES

    state = _load_raw()
    state["daily"] = {
        "date":       _today_et(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "regime":     regime,
        "label":      reg_ctx.get("label", "Unknown"),
        "day_active": day_active,
    }
    _save_raw(state)
    logger.info(
        "Daily regime saved: %s | day_active=%s | date=%s",
        regime, day_active, state["daily"]["date"],
    )


# ── Readers ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Return the full state dict. Keys: 'weekly', 'daily' (may be absent)."""
    return _load_raw()


def is_market_active() -> bool:
    """
    True if the weekly gate is open for this week.
    Falls back to False if no weekly check has run yet.
    """
    state = _load_raw()
    weekly = state.get("weekly", {})
    if not weekly:
        return False
    # Valid for the whole current week
    if weekly.get("week_of") != _week_of_et():
        logger.debug("Weekly state is from a previous week — market_active defaults False")
        return False
    return bool(weekly.get("market_active", False))


def is_day_active() -> bool:
    """
    True if both gates are open: market_active (weekly) AND day_active (daily).
    """
    if not is_market_active():
        return False
    state = _load_raw()
    daily = state.get("daily", {})
    if not daily:
        return False
    if daily.get("date") != _today_et():
        logger.debug("Daily state is from a previous day — day_active defaults False")
        return False
    return bool(daily.get("day_active", False))


def has_weekly_run_this_week() -> bool:
    """True if the weekly check has already fired this week."""
    state = _load_raw()
    return state.get("weekly", {}).get("week_of") == _week_of_et()


def has_daily_run_today() -> bool:
    """True if the daily pre-market check has already fired today."""
    state = _load_raw()
    return state.get("daily", {}).get("date") == _today_et()
