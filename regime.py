"""
modules/regime.py — SPY-based market regime classification (v3).

Classifies the current market into one of five regimes using SPY daily data.
Results are cached for up to 4 hours to avoid redundant yfinance calls.

Regime labels (priority order — first match wins):
  BEAR          : price <= SMA200 OR SMA50 <= SMA200
  CORRECTION    : price > SMA200 AND dd_from_60d_high <= -5%
  CONSOLIDATION : SMA50 within 3% of SMA200 AND |SMA200_slope_20| < 0.4%
  BULL_HIGH_VOL : golden cross BUT elevated volatility (ATR_rank > 70th pct or roc20 < -3%)
  BULL_TREND    : clean uptrend with normal volatility

A 3-bar mode filter smooths the raw regime series to avoid single-bar flips.

Public interface:
  TARGET_REGIMES  : set of regimes where alerts are enabled
  REGIME_LABELS   : human-readable labels for each regime key
  get_current_regime()  -> str
  is_target_regime()    -> bool
  get_regime_context()  -> dict
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Public constants ───────────────────────────────────────────────────────────

TARGET_REGIMES: set = {"BULL_HIGH_VOL", "CORRECTION"}

REGIME_LABELS: dict = {
    "BULL_TREND":    "Bull Trend",
    "BULL_HIGH_VOL": "Bull High-Vol",
    "CONSOLIDATION": "Consolidation",
    "CORRECTION":    "Correction / Dip",
    "BEAR":          "Bear / Breakdown",
    "UNKNOWN":       "Unknown",
}

# ── Cache ──────────────────────────────────────────────────────────────────────

_cache_lock   = threading.Lock()
_cached_regime_series: Optional[pd.Series]   = None   # index = DatetimeIndex
_cached_spy_data:      Optional[pd.DataFrame] = None
_cache_built_at:       Optional[datetime]     = None
_CACHE_TTL = timedelta(hours=4)

# ── Indicator parameters ───────────────────────────────────────────────────────
_SMA50_PERIOD   = 50
_SMA200_PERIOD  = 200
_ATR_PERIOD     = 14
_ATR_RANK_WIN   = 252    # rolling window for ATR percentile rank
_ATR_RANK_MIN   = 60     # min periods for rank calculation
_DD_LOOKBACK    = 60     # bars for drawdown-from-high calculation
_ROC_LOOKBACK   = 20     # bars for rate-of-change
_CONSOL_BAND    = 0.03   # 3% — SMA50 within this of SMA200 = consolidation
_CONSOL_SLOPE   = 0.004  # 0.4% — max SMA200 slope for consolidation
_ATR_RANK_HIGH  = 70     # percentile threshold for "high volatility"
_DD_THRESHOLD   = -0.05  # -5% drawdown from 60d high = correction
_ROC_THRESHOLD  = -0.03  # -3% 20-bar ROC = deteriorating momentum
_SMOOTH_WINDOW  = 3      # 3-bar mode filter


# ── Private helpers ────────────────────────────────────────────────────────────

def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder ATR via ewm alpha=1/period."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _percentile_rank(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    """
    For each bar, compute the percentile rank of the current value
    within the rolling window of the past `window` bars.
    Returns values in [0, 100].
    """
    def _rank(arr: np.ndarray) -> float:
        if len(arr) < 2:
            return 50.0
        return float(np.sum(arr[:-1] <= arr[-1]) / (len(arr) - 1) * 100)

    return series.rolling(window, min_periods=min_periods).apply(_rank, raw=True)


def _mode_filter(series: pd.Series, window: int) -> pd.Series:
    """Apply a rolling mode filter of width `window` to a categorical Series."""
    values = series.values
    result = values.copy()
    for i in range(len(values)):
        start = max(0, i - window + 1)
        window_vals = values[start : i + 1]
        # Find mode (most frequent value)
        unique, counts = np.unique(window_vals, return_counts=True)
        result[i] = unique[np.argmax(counts)]
    return pd.Series(result, index=series.index)


def _classify_regime(spy_df: pd.DataFrame) -> pd.Series:
    """
    Compute the regime label for every bar in spy_df.
    Returns a pd.Series of regime strings with the same DatetimeIndex.
    """
    close  = spy_df["Close"].astype(float)
    high   = spy_df["High"].astype(float)
    low    = spy_df["Low"].astype(float)

    sma50  = close.rolling(_SMA50_PERIOD).mean()
    sma200 = close.rolling(_SMA200_PERIOD).mean()
    atr14  = _wilder_atr(high, low, close, _ATR_PERIOD)

    # ATR as fraction of close, then percentile rank
    atr_norm = atr14 / close
    atr_rank = _percentile_rank(atr_norm, _ATR_RANK_WIN, _ATR_RANK_MIN)

    # 60-bar rolling max for drawdown
    roll_high_60 = close.rolling(_DD_LOOKBACK).max()
    dd_from_high = (close - roll_high_60) / roll_high_60   # negative values

    # 20-bar rate of change
    roc20 = (close / close.shift(_ROC_LOOKBACK)) - 1.0

    # SMA200 slope over 20 bars
    sma200_slope = (sma200 - sma200.shift(20)) / sma200.shift(20)

    regimes = []
    for i in range(len(close)):
        c   = close.iloc[i]
        s50 = sma50.iloc[i]
        s200 = sma200.iloc[i]
        ar   = atr_rank.iloc[i] if not np.isnan(atr_rank.iloc[i]) else 50.0
        dd   = dd_from_high.iloc[i] if not np.isnan(dd_from_high.iloc[i]) else 0.0
        roc  = roc20.iloc[i] if not np.isnan(roc20.iloc[i]) else 0.0
        slope = sma200_slope.iloc[i] if not np.isnan(sma200_slope.iloc[i]) else 0.0

        if np.isnan(c) or np.isnan(s50) or np.isnan(s200):
            regimes.append("UNKNOWN")
            continue

        # Priority order: BEAR → CORRECTION → CONSOLIDATION → BULL_HIGH_VOL → BULL_TREND
        if c <= s200 or s50 <= s200:
            regimes.append("BEAR")
        elif c > s200 and dd <= _DD_THRESHOLD:
            regimes.append("CORRECTION")
        elif abs((s50 - s200) / s200) <= _CONSOL_BAND and abs(slope) < _CONSOL_SLOPE:
            regimes.append("CONSOLIDATION")
        elif s50 > s200 and c > s200 and (ar > _ATR_RANK_HIGH or roc < _ROC_THRESHOLD):
            regimes.append("BULL_HIGH_VOL")
        else:
            regimes.append("BULL_TREND")

    raw = pd.Series(regimes, index=close.index)
    return _mode_filter(raw, _SMOOTH_WINDOW)


def _build_cache() -> None:
    """Download SPY, compute regimes, store in module-level cache."""
    global _cached_regime_series, _cached_spy_data, _cache_built_at
    try:
        raw = yf.download(
            "SPY",
            period="1y",
            interval="1d",
            progress=False,
            auto_adjust=True,
            multi_level_index=False,
        )
        if raw is None or raw.empty:
            logger.warning("regime: could not download SPY data.")
            return

        regime_series = _classify_regime(raw)
        _cached_spy_data      = raw
        _cached_regime_series = regime_series
        _cache_built_at       = datetime.now(timezone.utc)
        logger.info(
            "Regime cache built. Most recent: %s (%s)",
            regime_series.iloc[-1],
            regime_series.index[-1].date() if hasattr(regime_series.index[-1], "date") else "",
        )
    except Exception as exc:
        logger.exception("regime: error building cache: %s", exc)


def _invalidate_cache() -> None:
    """
    Force the cache to be treated as stale so the next call to
    get_current_regime / get_regime_context re-downloads SPY data.
    Must be called while holding _cache_lock.
    """
    global _cache_built_at
    _cache_built_at = None


def _cache_is_stale() -> bool:
    """Return True if cache is absent or older than _CACHE_TTL."""
    if _cache_built_at is None or _cached_regime_series is None:
        return True
    return datetime.now(timezone.utc) - _cache_built_at > _CACHE_TTL


# ── Public interface ───────────────────────────────────────────────────────────

def get_current_regime() -> str:
    """
    Returns the smoothed regime label for the most recent bar.
    Downloads SPY via yfinance (period='1y', interval='1d').
    Uses module-level cache; rebuilds if stale (>4h) or missing.
    Returns 'UNKNOWN' on any error.
    """
    with _cache_lock:
        if _cache_is_stale():
            _build_cache()
        if _cached_regime_series is None or _cached_regime_series.empty:
            return "UNKNOWN"
        return str(_cached_regime_series.iloc[-1])


def is_target_regime() -> bool:
    """Returns True if get_current_regime() is in TARGET_REGIMES."""
    return get_current_regime() in TARGET_REGIMES


def get_regime_context(force_refresh: bool = False) -> dict:
    """
    Returns a dict for inclusion in Telegram alert messages.
    Returns empty dict on error.

    Args:
        force_refresh: When True, invalidates the in-memory cache before
            fetching so the call always downloads fresh SPY data from
            yfinance.  Pass True from the post-close routine to guarantee
            confirmed closing prices are used (Issue #3).
    """
    try:
        with _cache_lock:
            if force_refresh:
                _invalidate_cache()
                logger.info("regime: cache invalidated for post-close force-refresh.")
            if _cache_is_stale():
                _build_cache()
            if _cached_regime_series is None or _cached_spy_data is None:
                return {}

            regime = str(_cached_regime_series.iloc[-1])

            spy_close  = float(_cached_spy_data["Close"].iloc[-1])
            sma50_s    = _cached_spy_data["Close"].astype(float).rolling(_SMA50_PERIOD).mean()
            sma200_s   = _cached_spy_data["Close"].astype(float).rolling(_SMA200_PERIOD).mean()
            spy_sma50  = float(sma50_s.iloc[-1])
            spy_sma200 = float(sma200_s.iloc[-1])

            atr14_s    = _wilder_atr(
                _cached_spy_data["High"].astype(float),
                _cached_spy_data["Low"].astype(float),
                _cached_spy_data["Close"].astype(float),
                _ATR_PERIOD,
            )
            atr_norm   = atr14_s / _cached_spy_data["Close"].astype(float)
            atr_rank_s = _percentile_rank(atr_norm, _ATR_RANK_WIN, _ATR_RANK_MIN)
            atr_rank_pct = float(atr_rank_s.iloc[-1]) if not np.isnan(atr_rank_s.iloc[-1]) else 50.0

        return {
            "regime":       regime,
            "label":        REGIME_LABELS.get(regime, regime),
            "is_target":    regime in TARGET_REGIMES,
            "spy_sma50":    round(spy_sma50,    2),
            "spy_sma200":   round(spy_sma200,   2),
            "spy_close":    round(spy_close,    2),
            "atr_rank_pct": round(atr_rank_pct, 1),
        }
    except Exception as exc:
        logger.exception("regime: get_regime_context error: %s", exc)
        return {}
