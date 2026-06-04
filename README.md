# 📈 Trading Alert Bot

A production-ready Telegram trading bot that monitors a dynamic watchlist of stock tickers, calculates technical indicators, and sends alerts — deployed on Railway via GitHub.

---

## Architecture Overview

```
bot.py                  ← Entry point; wires Telegram app + background task
modules/
  commands.py           ← /start, /add, /remove, /watchlist handlers
  scanner.py            ← Async background loop; runs every hour
  analysis.py           ← yfinance data fetch + SMA/EMA calculations
  watchlist.py          ← JSON-backed persistent watchlist (watchlist.json)
  trading212.py         ← Trading 212 REST API client (health-check + positions)
```

---

## Alert Logic

For every ticker in the watchlist, each scan cycle:

1. **Uptrend filter**: Only proceed if `Current Price > 200 SMA`
2. **Proximity check**: Is the price within **0.5 %** of any of these MAs?
   - 200 SMA
   - 62 EMA
   - 79 EMA
3. **Cooldown**: Suppress repeated alerts for the same ticker within 4 hours

---

## Local Development

### 1. Clone & install

```bash
git clone https://github.com/your-user/trading-bot.git
cd trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID at minimum
```

### 3. Run

```bash
python bot.py
```

---

## Railway Deployment

### Step 1 — Push to GitHub

```bash
git add .
git commit -m "Initial trading bot"
git push origin main
```

### Step 2 — Create Railway project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repository

### Step 3 — Set environment variables

In Railway → **Variables** tab, add:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Chat/channel ID for alerts |
| `TRADING212_API_KEY` | ⬜ | T212 API key (optional) |
| `T212_ENV` | ⬜ | `live` or `demo` (default: `live`) |
| `SCAN_INTERVAL` | ⬜ | Scan frequency in seconds (default: `3600`) |
| `ALERT_COOLDOWN_HOURS` | ⬜ | Cooldown between same-ticker alerts (default: `4`) |
| `MARKET_OPEN_UTC` | ⬜ | UTC hour scans start (default: `13` = 09:00 ET) |
| `MARKET_CLOSE_UTC` | ⬜ | UTC hour scans stop (default: `21` = 17:00 ET) |
| `WATCHLIST_PATH` | ⬜ | Path to JSON store (default: `watchlist.json`) |

### Step 4 — Deploy

Railway will auto-detect the `Procfile` and run:
```
worker: python bot.py
```

The bot will send a startup message to your Telegram chat on first boot.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` or `/help` | Show instructions and current watchlist |
| `/add AAPL` | Add AAPL to the watchlist (validates ticker first) |
| `/remove AAPL` | Remove AAPL from the watchlist |
| `/watchlist` | Show all tracked tickers with price/trend/last alert |

---

## Getting Your Telegram Chat ID

1. Add your bot to a group, or start a direct chat with it
2. Send `/start`
3. Open: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Find `"chat":{"id": -1009876543210}` in the response
5. Use that number as `TELEGRAM_CHAT_ID`

---

## Trading 212 API Key

1. Log into Trading 212 → **Settings** → **API** tab
2. Generate a new key with **read** permissions (no trading permissions needed for alerts)
3. Paste it as `TRADING212_API_KEY` in Railway
4. Set `T212_ENV=demo` to connect to your paper trading account

---

## Extending the Bot

- **Add more indicators**: Edit `modules/analysis.py` — add MACD, RSI, Bollinger Bands using pandas `.ewm()` / `.rolling()`
- **Change MA periods**: Modify `SMA_PERIOD`, `EMA_PERIOD_A`, `EMA_PERIOD_B` constants in `analysis.py`
- **Intraday scanning**: Change `CANDLE_INTERVAL = "1h"` and `CANDLE_PERIOD = "60d"` in `analysis.py`, and reduce `SCAN_INTERVAL` to `300` (5 min)
- **Persist across Railway sleeps**: Mount a Railway Volume to the `/app` directory and set `WATCHLIST_PATH=/app/watchlist.json`

---

## Notes

- **yfinance** data has a ~15-minute delay for free users; this bot is designed for **daily/swing trading signals**, not HFT
- Railway free-tier containers may sleep after inactivity — upgrade to a paid plan or use a `keep-alive` cron job if needed
- The `watchlist.json` file is written to the container's local filesystem; Railway ephemeral storage resets on redeploy. To persist it, use a **Railway Volume** or an external store (Redis, Postgres)
