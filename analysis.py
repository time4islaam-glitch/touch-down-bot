"""
modules/analysis.py — Market data fetching & technical indicator engine (v3).

Fetches OHLCV data via yfinance and computes:
  • 200-period SMA
  • 62-period  EMA
  • 79-period  EMA
  • 14-period  ATR  (Wilder, ewm alpha=1/14)
  • 20-period  Volume MA

Entry quality checks (all 8 must pass to fire an alert):
  1. Uptrend          — Close > 200 SMA
  2. SMA200 slope     — SMA200[-1] > SMA200[-21]  (20-bar lookback)
  3. MA proximity     — candle LOW within 1.5% of 62 EMA, 79 EMA, or 200 SMA
  4. Bullish candle   — green close AND closes in upper 50% of range
  5. Volume           — last candle volume >= 20-day average volume
  6. EMA slope        — 62 EMA rising over last 5 bars
  7. Clean structure  — no close > 1.5% below 79 EMA in prior 5 bars
  8. Not overextended — price not > 10% above 200 SMA

Stop loss guidance (returned in result for display in alert):
  • Hard stop         : EMA_79 − 1.0 × ATR_14
  • Primary stop      : EMA_79
  • Catastrophic      : 200 SMA
  • Partial target    : entry + 2 × (entry − hard_stop)
  • Trail activation  : entry × 1.03
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
ATR_PERIOD   = 14

# Minimum rows needed: 200 SMA + 20-bar slope lookback + buffer
MIN_ROWS = 230

# ── Entry filter constants ─────────────────────────────────────────────────────
PROXIMITY_THRESHOLD   = 0.015   # 1.5% — low must be within this of an MA
MAX_SMA_DISTANCE      = 0.10    # 10%  — price must not be >10% above 200 SMA
EMA_SLOPE_LOOKBACK    = 5       # bars — how far back to compare 62 EMA slope
SMA_SLOPE_LOOKBACK    = 20      # bars — how far back to compare 200 SMA slope
STRUCTURE_LOOKBACK    = 5       # bars — bars to check for clean EMA support
STRUCTURE_TOLERANCE   = 0.015   # 1.5% — max allowed breach below 79 EMA
VOLUME_MA_PERIOD      = 20      # bars — rolling window for average volume

# ── Data fetch settings ────────────────────────────────────────────────────────
CANDLE_INTERVAL = "1d"
CANDLE_PERIOD   = "2y"   # comfortably covers 200+ bars


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SignalChecks:
    """
    Boolean result of each individual entry quality check (8 total).
    A False on any check means the alert is suppressed.
    """
    uptrend:         bool = False   # Close > 200 SMA
    sma200_slope:    bool = False   # SMA200 trending up over 20 bars
    ma_proximity:    bool = False   # low within 1.5% of 62 EMA, 79 EMA, or 200 SMA
    bullish_candle:  bool = False   # green close in upper half of candle range
    volume_ok:       bool = False   # volume >= 20-day average
    ema_slope_ok:    bool = False   # 62 EMA sloping up over last 5 bars
    clean_structure: bool = False   # no close > 1.5% below 79 EMA in prior 5 bars
    not_overextended:bool = False   # price not >10% above 200 SMA

    @property
    def score(self) -> int:
        """Number of checks that passed (0–8)."""
        return sum([
            self.uptrend,
            self.sma200_slope,
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
        return self.score == 8


@dataclass
class StopLevels:
    """Derived stop loss and target price levels for display in the alert."""
    hard_stop:         float = 0.0   # EMA_79 − 1.0 × ATR_14
    primary_stop:      float = 0.0   # EMA_79 value — close below = exit
    catastrophic_stop: float = 0.0   # 200 SMA value — structural backstop
    partial_target:    float = 0.0   # entry + 2 × (entry − hard_stop)
    trail_activation:  float = 0.0   # entry × 1.03
    atr14:             float = 0.0   # ATR_14 value
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
    atr_14:  Optional[float] = None

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
    if score == 8:
        return "⭐ PERFECT"
    if score >= 6:
        return "🟢 STRONG"
    if score >= 4:
        return "🟡 MODERATE"
    return "🔴 WEAK"


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's ATR using ewm with alpha=1/period.
    True Range = max(H-L, |H-C_prev|, |L-C_prev|)
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


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
            multi_level_index=False,
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
        sma_200_series  = closes.rolling(SMA_PERIOD).mean()
        ema_62_series   = closes.ewm(span=EMA_PERIOD_A, adjust=False).mean()
        ema_79_series   = closes.ewm(span=EMA_PERIOD_B, adjust=False).mean()
        atr_14_series   = _wilder_atr(highs, lows, closes, ATR_PERIOD)
        vol_ma20_series = volumes.rolling(VOLUME_MA_PERIOD).mean()

        sma_200       = float(sma_200_series.iloc[-1])
        ema_62        = float(ema_62_series.iloc[-1])
        ema_79        = float(ema_79_series.iloc[-1])
        atr_14        = float(atr_14_series.iloc[-1])
        current_price = float(closes.iloc[-1])
        last_low      = float(lows.iloc[-1])
        vol_current   = float(volumes.iloc[-1])
        vol_ma20      = float(vol_ma20_series.iloc[-1])

        result.current_price  = current_price
        result.sma_200        = round(sma_200,  4)
        result.ema_62         = round(ema_62,   4)
        result.ema_79         = round(ema_79,   4)
        result.atr_14         = round(atr_14,   4)
        result.volume_current = round(vol_current, 0)
        result.volume_ma20    = round(vol_ma20,    0)

        checks = SignalChecks()

        # ── 3. Check 1 — Uptrend (Close > 200 SMA) ──────────────────────────
        checks.uptrend = current_price > sma_200

        # ── 4. Check 2 — SMA200 slope (rising over 20 bars) ─────────────────
        sma_200_prev = float(sma_200_series.iloc[-(SMA_SLOPE_LOOKBACK + 1)])
        checks.sma200_slope = sma_200 > sma_200_prev

        # ── 5. Check 3 — MA proximity (low within 1.5% of any MA) ───────────
        ma_map = {
            f"62 EMA (${ema_62:.2f})":   ema_62,
            f"79 EMA (${ema_79:.2f})":   ema_79,
            f"200 SMA (${sma_200:.2f})": sma_200,
        }
        for label, ma_val in ma_map.items():
            if _pct_diff(last_low, ma_val) <= PROXIMITY_THRESHOLD:
                result.triggered_mas.append(label)

        checks.ma_proximity = len(result.triggered_mas) > 0

        # ── 6. Check 4 — Bullish candle confirmation ─────────────────────────
        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        last_high  = float(highs.iloc[-1])
        candle_range = last_high - last_low

        if candle_range > 0:
            close_position = (last_close - last_low) / candle_range
            checks.bullish_candle = (last_close > prev_close) and (close_position >= 0.50)
        else:
            # Doji — treat as not bullish
            checks.bullish_candle = False

        # ── 7. Check 5 — Volume confirmation ────────────────────────────────
        checks.volume_ok = vol_current >= vol_ma20

        # ── 8. Check 6 — EMA slope (62 EMA trending up over 5 bars) ─────────
        ema_62_prev = float(ema_62_series.iloc[-(EMA_SLOPE_LOOKBACK + 1)])
        checks.ema_slope_ok = ema_62 > ema_62_prev

        # ── 9. Check 7 — Clean structure ────────────────────────────────────
        # bars [-6..-2]: the 5 candles BEFORE the current one
        recent_closes = closes.iloc[-(STRUCTURE_LOOKBACK + 1):-1]
        recent_ema79  = ema_79_series.iloc[-(STRUCTURE_LOOKBACK + 1):-1]
        # pct breach below EMA_79 for each bar (clipped to 0 if above)
        breach = ((recent_ema79 - recent_closes) / recent_ema79).clip(lower=0)
        checks.clean_structure = bool((breach <= STRUCTURE_TOLERANCE).all())

        # ── 10. Check 8 — Not overextended ──────────────────────────────────
        sma_distance = (current_price - sma_200) / sma_200
        checks.not_overextended = sma_distance <= MAX_SMA_DISTANCE

        result.checks = checks

        # ── 11. Compute stop loss levels ─────────────────────────────────────
        hard_stop        = ema_79 - 1.0 * atr_14
        partial_target   = current_price + 2.0 * (current_price - hard_stop)
        trail_activation = current_price * 1.03
        risk_pct         = ((current_price - hard_stop) / current_price) * 100

        result.stops = StopLevels(
            hard_stop         = round(hard_stop,        4),
            primary_stop      = round(ema_79,            4),
            catastrophic_stop = round(sma_200,           4),
            partial_target    = round(partial_target,    4),
            trail_activation  = round(trail_activation,  4),
            atr14             = round(atr_14,            4),
            risk_pct          = round(risk_pct,          2),
        )

        logger.info(
            "%s | price=%.2f | SMA200=%.2f | EMA62=%.2f | EMA79=%.2f | "
            "ATR14=%.2f | score=%d/8 | all_pass=%s | proximity=%s",
            ticker, current_price, sma_200, ema_62, ema_79,
            atr_14, checks.score, checks.all_pass, result.triggered_mas,
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
            multi_level_index=False,
        )
        return data is not None and not data.empty
    except Exception as exc:
        logger.warning("Ticker validation failed for %s: %s", ticker, exc)
        return False
