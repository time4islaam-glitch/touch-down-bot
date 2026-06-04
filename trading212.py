"""
modules/trading212.py — Trading 212 REST API client.

Provides a lightweight wrapper around the official Trading 212 API v0.
Used on startup to verify connectivity and fetch account metadata.

Docs: https://t212public-api-docs.redoc.ly/

Authentication: API Key passed as the `Authorization` header value
(Bearer token style — the raw key IS the token, no "Bearer" prefix needed).

Environment variables:
  TRADING212_API_KEY  — Your T212 API key (required to enable integration).
  T212_ENV            — "live" (default) or "demo"
"""

import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Base URLs for live and paper/demo environments
_BASE_URLS = {
    "live": "https://live.trading212.com/api/v0",
    "demo": "https://demo.trading212.com/api/v0",
}

# Timeout for HTTP requests (seconds)
REQUEST_TIMEOUT = 15


class Trading212Client:
    """
    Thin synchronous client for Trading 212's REST API.

    Methods return parsed JSON dicts or raise requests.HTTPError on failure.
    All methods are intentionally synchronous; call them from a thread pool
    (loop.run_in_executor) if you need to await them in async contexts.
    """

    def __init__(self) -> None:
        self.api_key: Optional[str] = os.environ.get("TRADING212_API_KEY")
        env = os.environ.get("T212_ENV", "live").lower()

        if env not in _BASE_URLS:
            logger.warning(
                "T212_ENV='%s' is invalid; defaulting to 'live'.", env
            )
            env = "live"

        self.base_url: str = _BASE_URLS[env]
        self.enabled: bool = bool(self.api_key)

        if self.enabled:
            logger.info(
                "Trading212Client initialised — env=%s, base=%s", env, self.base_url
            )
        else:
            logger.info(
                "TRADING212_API_KEY not set — T212 integration disabled."
            )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        """Build the auth headers required by T212."""
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """
        Perform a GET request against the T212 API.
        Raises requests.HTTPError on non-2xx responses.
        """
        if not self.enabled:
            raise RuntimeError(
                "Trading212Client is disabled (TRADING212_API_KEY not set)."
            )

        url = f"{self.base_url}{path}"
        response = requests.get(
            url,
            headers=self._headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    # ── Public API methods ─────────────────────────────────────────────────────

    def get_account_metadata(self) -> Dict[str, Any]:
        """
        GET /equity/account/info
        Returns account metadata: currency, ID, account type, etc.
        Use this as a connectivity health-check on startup.
        """
        return self._get("/equity/account/info")

    def get_account_cash(self) -> Dict[str, Any]:
        """
        GET /equity/account/cash
        Returns available cash, blocked funds, free funds, etc.
        """
        return self._get("/equity/account/cash")

    def get_open_positions(self) -> list:
        """
        GET /equity/portfolio
        Returns a list of all currently open positions with
        quantity, average price, current price, P&L, etc.
        """
        return self._get("/equity/portfolio")

    def get_position(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        GET /equity/portfolio/{ticker}
        Returns position details for a specific instrument or None
        if no open position exists.
        """
        try:
            return self._get(f"/equity/portfolio/{ticker}")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None   # No open position — that's fine
            raise

    def get_instruments(self) -> list:
        """
        GET /equity/metadata/instruments
        Returns the full list of tradeable instruments with ISIN,
        type, currency, min/max quantity, etc.

        ⚠️  This endpoint can return a large payload (~4 MB).
        Cache the result if you call it frequently.
        """
        return self._get("/equity/metadata/instruments")

    def get_exchanges(self) -> list:
        """
        GET /equity/metadata/exchanges
        Returns exchange metadata including working schedules.
        """
        return self._get("/equity/metadata/exchanges")

    def get_orders(self) -> list:
        """
        GET /equity/orders
        Returns all currently active (pending/working) orders.
        """
        return self._get("/equity/orders")

    # ── Convenience summary ────────────────────────────────────────────────────

    def account_summary(self) -> Dict[str, Any]:
        """
        Composite call: fetch both account info and cash in one dict.
        Useful for the startup health-check log message.
        """
        info = self.get_account_metadata()
        cash = self.get_account_cash()
        return {
            "account_id":       info.get("id"),
            "currency":         info.get("currencyCode"),
            "free_funds":       cash.get("free"),
            "blocked_funds":    cash.get("blocked"),
            "total_cash":       cash.get("total"),
        }
