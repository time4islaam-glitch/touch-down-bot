"""
modules/analysis.py — Market data fetching & technical indicator engine.

Fetches OHLCV data via yfinance and computes:
  • 200-period SMA
  • 62-period  EMA
  • 79-period  EMA

Entry quality checks (all must pass to fire an alert):
  1. Uptrend          — price > 200 SMA
  2. MA proximity     — price within 0.5% of 62 EMA or 79 EMA
  3. Bullish candle   — last candle is green AND closes in upper 50% of its range
  4. Volume           — last candle volume >= 20-day average volume
  5. EMA slope        — 62 EMA is higher than it was 5 bars ago (trend momentum)
  6. Clean structure  — no daily close below 79 EMA in the last 5 bars
  7. Not overextended — price is not more than 10% above 200 SMA

Stop loss guidance (returned in result for display in alert):
  • Primary stop  : daily close below 79 EMA  (thesis invalidation)
  • Hard stop     : 0.5% buffer below 79 EMA  (exact price level)
  • Catastrophic  : daily close below 200 SMA (structural breakdown)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Indicator periods ──────────────────────────────────────────────────────────
SMA_PERIOD   = 200
EMA_PERIOD_A = 62
EMA_PERIOD_B = 79

# Minimum rows needed to compute all indicators reliably
MIN_ROWS = SMA_PERIOD + 10

# ── Entry filter constants ─────────────────────────────────────────────────────
PROXIMITY_THRESHOLD   = 0.005   # 0.5% — price must be this close to an EMA
MAX_SMA_DISTANCE      = 0.10    # 10%  — price must not be >10% above 200 SMA
EMA_SLOPE_LOOKBACK    = 5       # bars — how far back to compare EMA slope
STRUCTURE_LOOKBACK    = 5       # bars — bars to check for clean EMA support
VOLUME_MA_PERIOD      = 20      # bars — rolling window for average volume

# ── Data fetch settings ────────────────────────────────────────────────────────
CANDLE_INTERVAL = "1d"
CANDLE_PERIOD   = "2y"   # comfortably covers 200+ bars


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SignalChecks:
    """
    Boolean result of each individual entry quality check.
    A False on any check means the alert is suppressed.
    """
    uptrend:        bool = False   # price > 200 SMA
    ma_proximity:   bool = False   # within 0.5% of 62 or 79 EMA
    bullish_candle: bool = False   # green close in upper half of candle range
    volume_ok:      bool = False   # volume >= 20-day average
    ema_slope_ok:   bool = False   # 62 EMA sloping up over last 5 bars
    clean_structure:bool = False   # no close below 79 EMA in last 5 bars
    not_overextended:bool= False   # price not >10% above 200 SMA

    @property
    def score(self) -> int:
        """Number of checks that passed (0-7)."""
        return sum([
            self.uptrend,
            self.ma_proximity,
            self.bullish_candle,
            self.volume_ok,
            self.ema_slope_ok,
            self.clean_structure,
            self.not_overextended,
        ])

    @property
    def all_pass(self) -> bool:
        """True only when every check passes — required for an alert."""
        return self.score == 7


@dataclass
class StopLevels:
    """Derived stop loss price levels for display in the alert."""
    primary_stop:      float = 0.0   # 79 EMA value — close below = exit
    hard_stop:         float = 0.0   # 79 EMA × 0.995 — intraday buffer
    catastrophic_stop: float = 0.0   # 200 SMA value — structural backstop
    risk_pct:          float = 0.0   # % distance from entry to hard stop


@dataclass
class AnalysisResult:
    """Complete analysis output for one ticker scan cycle."""

    ticker:        str
    timestamp:     datetime
    current_price: float

    # Indicators
    sma_200: Optional[float] = None
    ema_62:  Optional[float] = None
    ema_79:  Optional[float] = None

    # Volume context
    volume_current: Optional[float] = None
    volume_ma20:    Optional[float] = None

    # Which MA(s) are within proximity threshold
    triggered_mas: list = field(default_factory=list)

    # Entry quality breakdown
    checks: SignalChecks = field(default_factory=SignalChecks)

    # Stop loss levels (populated when checks.all_pass)
    stops: StopLevels = field(default_factory=StopLevels)

    # Non-None means something went wrong during data fetch / calc
    error: Optional[str] = None


# ── Helper functions ───────────────────────────────────────────────────────────

def _pct_diff(price: float, ma: float) -> float:
    """Absolute fractional distance between price and an MA level."""
    return abs(price - ma) / ma


def _score_label(score: int) -> str:
    """Human-readable quality label for the signal score."""
    if score == 7:
        return "⭐ PERFECT"
    if score >= 5:
        return "🟢 STRONG"
    if score >= 3:
        return "🟡 MODERATE"
    return "🔴 WEAK"


# ── Main analysis function ─────────────────────────────────────────────────────

def fetch_and_analyse(ticker: str) -> AnalysisResult:
    """
    Download OHLCV history for *ticker*, compute all indicators, and
    evaluate every entry quality check.

    Returns a fully populated AnalysisResult.
    On data/fetch failure, .error is set and checks remain False.
    """
    ts = datetime.utcnow()
    result = AnalysisResult(ticker=ticker, timestamp=ts, current_price=0.0)

    try:
        # ── 1. Download OHLCV ────────────────────────────────────────────────
        raw: pd.DataFrame = yf.download(
            ticker,
            period=CANDLE_PERIOD,
            interval=CANDLE_INTERVAL,
            progress=False,
            auto_adjust=True,
            multi_level_column=False,
        )

        if raw is None or raw.empty:
            result.error = f"No data returned for {ticker}"
            logger.warning(result.error)
            return result

        if len(raw) < MIN_ROWS:
            result.error = (
                f"Insufficient history ({len(raw)} bars) for {ticker}; "
                f"need >= {MIN_ROWS}."
            )
            logger.warning(result.error)
            return result

        # Ensure columns are clean floats
        closes:  pd.Series = raw["Close"].astype(float).dropna()
        highs:   pd.Series = raw["High"].astype(float).dropna()
        lows:    pd.Series = raw["Low"].astype(float).dropna()
        volumes: pd.Series = raw["Volume"].astype(float).dropna()

        # ── 2. Compute indicators ────────────────────────────────────────────
        sma_200_series = closes.rolling(SMA_PERIOD).mean()
        ema_62_series  = closes.ewm(span=EMA_PERIOD_A, adjust=False).mean()
        ema_79_series  = closes.ewm(span=EMA_PERIOD_B, adjust=False).mean()
        vol_ma20_series = volumes.rolling(VOLUME_MA_PERIOD).mean()

        sma_200       = float(sma_200_series.iloc[-1])
        ema_62        = float(ema_62_series.iloc[-1])
        ema_79        = float(ema_79_series.iloc[-1])
        current_price = float(closes.iloc[-1])
        vol_current   = float(volumes.iloc[-1])
        vol_ma20      = float(vol_ma20_series.iloc[-1])

        result.current_price  = current_price
        result.sma_200        = round(sma_200, 4)
        result.ema_62         = round(ema_62,  4)
        result.ema_79         = round(ema_79,  4)
        result.volume_current = round(vol_current, 0)
        result.volume_ma20    = round(vol_ma20,    0)

        checks = SignalChecks()

        # ── 3. Check 1 — Uptrend (price > 200 SMA) ──────────────────────────
        checks.uptrend = current_price > sma_200

        # ── 4. Check 2 — MA proximity ────────────────────────────────────────
        # Populate triggered_mas regardless (used in alert message detail)
        ma_map = {
            f"62 EMA (${ema_62:.2f})": ema_62,
            f"79 EMA (${ema_79:.2f})": ema_79,
            f"200 SMA (${sma_200:.2f})": sma_200,
        }
        for label, ma_val in ma_map.items():
            if _pct_diff(current_price, ma_val) <= PROXIMITY_THRESHOLD:
                result.triggered_mas.append(label)

        checks.ma_proximity = len(result.triggered_mas) > 0

        # ── 5. Check 3 — Bullish candle confirmation ─────────────────────────
        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        last_high  = float(highs.iloc[-1])
        last_low   = float(lows.iloc[-1])
        candle_range = last_high - last_low

        if candle_range > 0:
            close_position = (last_close - last_low) / candle_range
            # Green candle AND closed in upper 50% of the bar's range
            checks.bullish_candle = (last_close > prev_close) and (close_position >= 0.50)
        else:
            # Doji — neutral, treat as not bullish
            checks.bullish_candle = False

        # ── 6. Check 4 — Volume confirmation ────────────────────────────────
        checks.volume_ok = vol_current >= vol_ma20

        # ── 7. Check 5 — EMA slope (62 EMA trending up) ─────────────────────
        ema_62_now  = float(ema_62_series.iloc[-1])
        ema_62_prev = float(ema_62_series.iloc[-(EMA_SLOPE_LOOKBACK + 1)])
        checks.ema_slope_ok = ema_62_now > ema_62_prev

        # ── 8. Check 6 — Clean structure (no close below 79 EMA last 5 bars) ─
        # We look at bars [-6 : -1] — i.e. the 5 candles BEFORE the current one
        recent_closes = closes.iloc[-(STRUCTURE_LOOKBACK + 1):-1]
        recent_ema79  = ema_79_series.iloc[-(STRUCTURE_LOOKBACK + 1):-1]
        # All recent closes must be AT OR ABOVE their corresponding 79 EMA
        checks.clean_structure = bool((recent_closes >= recent_ema79 * 0.99).all())

        # ── 9. Check 7 — Not overextended (price <= 200 SMA × 1.10) ─────────
        sma_distance = (current_price - sma_200) / sma_200
        checks.not_overextended = sma_distance <= MAX_SMA_DISTANCE

        result.checks = checks

        # ── 10. Compute stop loss levels ─────────────────────────────────────
        hard_stop = ema_79 * 0.995
        risk_pct  = ((current_price - hard_stop) / current_price) * 100

        result.stops = StopLevels(
            primary_stop      = round(ema_79,  4),
            hard_stop         = round(hard_stop, 4),
            catastrophic_stop = round(sma_200, 4),
            risk_pct          = round(risk_pct, 2),
        )

        logger.info(
            "%s | price=%.2f | SMA200=%.2f | EMA62=%.2f | EMA79=%.2f | "
            "score=%d/7 | all_pass=%s | proximity=%s",
            ticker, current_price, sma_200, ema_62, ema_79,
            checks.score, checks.all_pass, result.triggered_mas,
        )

    except Exception as exc:
        result.error = str(exc)
        logger.exception("Error analysing %s: %s", ticker, exc)

    return result


def validate_ticker(ticker: str) -> bool:
    """
    Quick sanity-check: download 5 days of data.
    Returns True if at least one row is returned.
    """
    try:
        data = yf.download(
            ticker,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            multi_level_column=False,
        )
        return data is not None and not data.empty
    except Exception as exc:
        logger.warning("Ticker validation failed for %s: %s", ticker, exc)
        return False
