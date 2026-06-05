"""
universe.py — Exchange universe scanning and pre-screening.

Downloads ticker lists for NASDAQ, NYSE, and/or LSE.
Applies cheap pre-screen filters (price, volume, above SMA50) before
the full 8-check analysis runs in scanner.py.

Public interface:
  async def refresh_universe(bot, chat_id: str) -> int
  def load_candidates() -> list[str]
  def save_candidates(tickers: list[str]) -> None

Constants (all overrideable via env vars):
  CANDIDATES_PATH          — path to candidates.json
  UNIVERSE_REFRESH_HOUR_UTC — UTC hour for nightly auto-refresh
  ENABLED_EXCHANGES        — comma-separated list of exchanges to scan
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CANDIDATES_PATH           = Path(os.environ.get("CANDIDATES_PATH", "candidates.json"))
UNIVERSE_REFRESH_HOUR_UTC = int(os.environ.get("UNIVERSE_REFRESH_HOUR", 4))
ENABLED_EXCHANGES         = os.environ.get("EXCHANGES", "NASDAQ,NYSE").upper().split(",")

# Pre-screen thresholds
MIN_PRICE        = 1.00          # ignore penny stocks
MIN_AVG_VOLUME   = 500_000       # 30-day avg shares/day
MAX_TICKERS_BATCH = 500          # max tickers processed per exchange per night
BATCH_SIZE       = 50            # tickers per yfinance batch download
BATCH_SLEEP_S    = 0.5           # seconds between batches

# Exchange CSV sources
NASDAQ_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/nasdaq-listings/master/data/nasdaq-listed.csv"
)
NYSE_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/nyse-other-listings/master/data/nyse-listed.csv"
)

# ── LSE — FTSE 350 curated static list ────────────────────────────────────────

LSE_TICKERS: list[str] = [
    # FTSE 100
    "AAL.L", "ABF.L", "ADM.L", "AHT.L", "ANTO.L", "AZN.L", "AUTO.L", "AV.L",
    "AVV.L", "BA.L", "BARC.L", "BATS.L", "BEZ.L", "BKG.L", "BP.L", "BVIC.L",
    "BRBY.L", "BT.L", "CCH.L", "CNA.L", "CPG.L", "CRDA.L", "DCC.L", "DGE.L",
    "DLG.L", "ENT.L", "EXPN.L", "EZJ.L", "FERG.L", "FLTR.L", "FRES.L", "GLEN.L",
    "GSK.L", "HLMA.L", "HL.L", "HSBA.L", "IAG.L", "IHG.L", "III.L", "IMB.L",
    "INF.L", "ITRK.L", "JD.L", "KGF.L", "LAND.L", "LGEN.L", "LLOY.L", "LNG.L",
    "LSE.L", "MKS.L", "MNDI.L", "MNG.L", "MRO.L", "NG.L", "NWG.L", "NXT.L",
    "OCDO.L", "PHNX.L", "PRU.L", "PSH.L", "PSN.L", "PSON.L", "REL.L", "RIO.L",
    "RKT.L", "RMV.L", "RR.L", "RS1.L", "RTO.L", "SBRY.L", "SDR.L", "SGE.L",
    "SGRO.L", "SHB.L", "SKG.L", "SMDS.L", "SMIN.L", "SMT.L", "SN.L", "SPX.L",
    "SSE.L", "STAN.L", "STJ.L", "SVT.L", "TSCO.L", "TW.L", "ULVR.L", "UU.L",
    "VOD.L", "WEIR.L", "WPP.L", "WTB.L",
    # FTSE 250 (selection of most liquid)
    "AGK.L", "AML.L", "ATG.L", "BBY.L", "BBOX.L", "BCG.L", "BDEV.L", "BHP.L",
    "BIFF.L", "BME.L", "BNZL.L", "BOO.L", "BOWL.L", "BTG.L", "BWY.L", "CAL.L",
    "CBPE.L", "CCC.L", "CINE.L", "CLG.L", "CLS.L", "CMC.L", "COB.L", "COPL.L",
    "CPH.L", "CSN.L", "CTY.L", "CVS.L", "DARK.L", "DCG.L", "DPLM.L", "ELM.L",
    "EMG.L", "EPIQ.L", "ESNT.L", "FCIT.L", "FGT.L", "FLT.L", "FOUR.L", "FSV.L",
    "GCP.L", "GNK.L", "GPOR.L", "GRI.L", "GRG.L", "GROW.L", "GTY.L", "HAS.L",
    "HIK.L", "HMSO.L", "HOC.L", "HOTC.L", "HUW.L", "HWDN.L", "IBST.L", "ICG.L",
    "IGG.L", "IMP.L", "ITV.L", "JFJ.L", "JMG.L", "JTC.L", "KIE.L", "LMP.L",
    "LRE.L", "LSEG.L", "MAB.L", "MAN.L", "MCLS.L", "MGAM.L", "MIDW.L", "MONY.L",
    "MPI.L", "MRC.L", "MTRO.L", "MUT.L", "NCC.L", "NETW.L", "NMC.L", "OCSL.L",
    "OML.L", "OPTI.L", "OSB.L", "OXB.L", "PAG.L", "PFC.L", "PGH.L", "PLUS.L",
    "PMO.L", "PNG.L", "POL.L", "PRV.L", "PTY.L", "PZC.L", "QQ.L", "RDSA.L",
    "RDSB.L", "RPC.L", "RPS.L", "RWA.L", "SAFE.L", "SAGA.L", "SDP.L", "SEPL.L",
    "SHI.L", "SIG.L", "SITI.L", "SIV.L", "SLA.L", "SLN.L", "SLT.L", "SNR.L",
    "SNWS.L", "SOM.L", "SPDI.L", "SQZ.L", "SRP.L", "SREI.L", "SRT.L", "SUS.L",
    "SVS.L", "SXS.L", "TCG.L", "TCAP.L", "TEM.L", "TLW.L", "TON.L", "TPK.L",
    "TRIG.L", "TRN.L", "TUI.L", "UBM.L", "UDG.L", "UIL.L", "ULVR.L", "UPR.L",
    "UTG.L", "VCT.L", "VLX.L", "WG.L", "WIZZ.L", "WMH.L", "WSP.L", "XAR.L",
    "XPP.L", "YCA.L", "YNGA.L", "ZIP.L",
]


# ── Ticker list fetchers ───────────────────────────────────────────────────────

def _fetch_nasdaq_tickers() -> list[str]:
    """Download NASDAQ-listed CSV and return symbol list."""
    try:
        df = pd.read_csv(NASDAQ_CSV_URL)
        symbols = df["Symbol"].dropna().astype(str).str.strip().tolist()
        # Basic sanity: alpha-only, 1–5 chars
        symbols = [s for s in symbols if s.isalpha() and 1 <= len(s) <= 5]
        logger.info("NASDAQ: fetched %d raw tickers", len(symbols))
        return symbols[:MAX_TICKERS_BATCH]
    except Exception as exc:
        logger.error("Failed to fetch NASDAQ ticker list: %s", exc)
        return []


def _fetch_nyse_tickers() -> list[str]:
    """Download NYSE-other-listings CSV and return symbol list."""
    try:
        df = pd.read_csv(NYSE_CSV_URL)
        symbols = df["ACT Symbol"].dropna().astype(str).str.strip().tolist()
        symbols = [s for s in symbols if s.isalpha() and 1 <= len(s) <= 5]
        logger.info("NYSE: fetched %d raw tickers", len(symbols))
        return symbols[:MAX_TICKERS_BATCH]
    except Exception as exc:
        logger.error("Failed to fetch NYSE ticker list: %s", exc)
        return []


def _fetch_lse_tickers() -> list[str]:
    """Return the curated FTSE 350 static list."""
    logger.info("LSE: using static list of %d tickers", len(LSE_TICKERS))
    return LSE_TICKERS[:MAX_TICKERS_BATCH]


# ── Pre-screen filter ──────────────────────────────────────────────────────────

def _prescreen_batch(tickers: list[str]) -> list[str]:
    """
    Download 60d/1d OHLCV for a batch of tickers and apply pre-screen filters:
      - last close > MIN_PRICE
      - 30-day avg volume > MIN_AVG_VOLUME
      - close > 50-period SMA (cheap uptrend proxy)

    Returns list of tickers that pass all three filters.
    """
    if not tickers:
        return []

    try:
        raw = yf.download(
            " ".join(tickers),
            period="60d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            multi_level_index=True,   # need multi-level when >1 ticker
        )
    except Exception as exc:
        logger.warning("yfinance batch download error: %s", exc)
        return []

    if raw is None or raw.empty:
        return []

    passing = []

    # Handle both single-ticker (flat) and multi-ticker (multi-level) results
    if isinstance(raw.columns, pd.MultiIndex):
        closes  = raw["Close"]
        volumes = raw["Volume"]
    else:
        # Single ticker — wrap in DataFrame with ticker as column name
        closes  = raw[["Close"]].rename(columns={"Close": tickers[0]})
        volumes = raw[["Volume"]].rename(columns={"Volume": tickers[0]})

    for ticker in tickers:
        if ticker not in closes.columns:
            continue
        close_s  = closes[ticker].dropna()
        volume_s = volumes[ticker].dropna()

        if len(close_s) < 51:   # need at least 51 bars for SMA50
            continue

        last_close   = float(close_s.iloc[-1])
        avg_vol_30   = float(volume_s.iloc[-30:].mean()) if len(volume_s) >= 30 else 0.0
        sma50        = float(close_s.rolling(50).mean().iloc[-1])

        if last_close < MIN_PRICE:
            continue
        if avg_vol_30 < MIN_AVG_VOLUME:
            continue
        if last_close <= sma50:
            continue

        passing.append(ticker)

    return passing


# ── Candidate persistence ─────────────────────────────────────────────────────

def load_candidates() -> list[str]:
    """Load candidates.json. Returns empty list if file is missing or corrupt."""
    try:
        if CANDIDATES_PATH.exists():
            with CANDIDATES_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("tickers", [])
    except Exception as exc:
        logger.warning("Could not load candidates.json: %s", exc)
    return []


def save_candidates(tickers: list[str]) -> None:
    """Atomic write to candidates.json via a temp file."""
    payload = {
        "tickers":      tickers,
        "count":        len(tickers),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        dir_ = CANDIDATES_PATH.parent
        with tempfile.NamedTemporaryFile(
            "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
        ) as tmp:
            json.dump(payload, tmp, indent=2)
            tmp_path = Path(tmp.name)
        tmp_path.replace(CANDIDATES_PATH)
        logger.info("Saved %d candidates to %s", len(tickers), CANDIDATES_PATH)
    except Exception as exc:
        logger.error("Failed to save candidates.json: %s", exc)


def get_last_refresh_time() -> str | None:
    """Returns the ISO timestamp of the last refresh, or None."""
    try:
        if CANDIDATES_PATH.exists():
            with CANDIDATES_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("refreshed_at")
    except Exception:
        pass
    return None


# ── Main refresh coroutine ────────────────────────────────────────────────────

async def refresh_universe(bot, chat_id: str) -> int:
    """
    Downloads ticker lists for each enabled exchange, applies pre-screen
    filters in batches, saves passing tickers to candidates.json.

    Returns the count of candidates saved.
    Sends a Telegram status message on completion.
    """
    logger.info(
        "Universe refresh starting — exchanges: %s",
        ", ".join(ENABLED_EXCHANGES),
    )

    loop = asyncio.get_running_loop()
    all_raw: list[str] = []

    # Collect raw tickers per exchange
    if "NASDAQ" in ENABLED_EXCHANGES:
        nasdaq = await loop.run_in_executor(None, _fetch_nasdaq_tickers)
        all_raw.extend(nasdaq)

    if "NYSE" in ENABLED_EXCHANGES:
        nyse = await loop.run_in_executor(None, _fetch_nyse_tickers)
        all_raw.extend(nyse)

    if "LSE" in ENABLED_EXCHANGES:
        lse = await loop.run_in_executor(None, _fetch_lse_tickers)
        all_raw.extend(lse)

    # Deduplicate
    all_raw = list(dict.fromkeys(all_raw))
    logger.info("Universe refresh: %d unique raw tickers before pre-screen", len(all_raw))

    # Pre-screen in batches of BATCH_SIZE
    candidates: list[str] = []
    batches = [all_raw[i : i + BATCH_SIZE] for i in range(0, len(all_raw), BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        passed = await loop.run_in_executor(None, _prescreen_batch, batch)
        candidates.extend(passed)
        logger.info(
            "Pre-screen batch %d/%d: %d/%d passed",
            idx, len(batches), len(passed), len(batch),
        )
        if idx < len(batches):
            await asyncio.sleep(BATCH_SLEEP_S)

    # Save
    save_candidates(candidates)

    # Telegram notification
    exchanges_str = ", ".join(ENABLED_EXCHANGES)
    msg = (
        f"🌐 *Universe Refresh Complete*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Exchanges scanned: `{exchanges_str}`\n"
        f"🔍 Raw tickers fetched: `{len(all_raw)}`\n"
        f"✅ Candidates passing pre-screen: `{len(candidates)}`\n"
        f"🕐 `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Failed to send universe refresh notification: %s", exc)

    return len(candidates)
