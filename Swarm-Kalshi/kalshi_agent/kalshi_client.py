"""
kalshi_client.py
================

Authenticated REST client for the Kalshi Exchange API.

Handles RSA-PSS request signing, automatic rate-limit back-off, cursor-based
pagination, and structured error handling.  Every public method returns parsed
JSON (``dict`` or ``list``) and raises ``KalshiAPIError`` on non-2xx responses.
"""

from __future__ import annotations

import base64
import datetime
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class KalshiAPIError(Exception):
    """Raised when the Kalshi API returns a non-2xx status code."""

    def __init__(self, status_code: int, message: str, response: Optional[requests.Response] = None):
        self.status_code = status_code
        self.message = message
        self.response = response
        super().__init__(f"HTTP {status_code}: {message}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KalshiClient:
    """
    Low-level, authenticated client for the Kalshi REST API (v2).

    Parameters
    ----------
    api_key_id : str
        The API Key ID displayed on the Kalshi dashboard.
    private_key_path : str
        File-system path to the PEM-encoded RSA private key (``.key``).
    base_url : str
        Full base URL including ``/trade-api/v2``.
    demo_mode : bool
        If *True*, the client targets the demo environment.
    """

    # Sensible defaults — overridden by ``config.yaml`` at runtime.
    DEFAULT_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
    _RATE_LIMIT_BACKOFF_BASE = 1.0  # seconds
    _MAX_RETRIES = 6

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        base_url: str = DEFAULT_BASE_URL,
        demo_mode: bool = True,
    ):
        self.api_key_id = api_key_id
        self.base_url = base_url.rstrip("/")
        self.demo_mode = demo_mode
        self._session = requests.Session()
        self._private_key = self._load_private_key(private_key_path)
        self._last_request_ts: float = 0.0
        # Safe default of 0.35s (~2.8 req/s per bot) so 4 concurrent bots stay within Kalshi global limit (~12 req/s)
        self._min_request_interval = 0.35

    # ------------------------------------------------------------------
    # Key loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_private_key(path: str):
        """Load an RSA private key from a PEM file."""
        key_path = Path(path).resolve()
        if not key_path.exists():
            raise FileNotFoundError(
                f"Private key file not found at '{key_path}'. "
                "Set 'api.private_key_path' in swarm_config.yaml to an absolute path, "
                "or set the KALSHI_KEY_PATH environment variable."
            )
        with open(key_path, "rb") as fh:
            return serialization.load_pem_private_key(
                fh.read(), password=None, backend=default_backend()
            )

    # ------------------------------------------------------------------
    # Request signing
    # ------------------------------------------------------------------

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Create an RSA-PSS SHA-256 signature for the request.

        The message to sign is ``timestamp_ms + METHOD + path`` where *path*
        is the URL path **without** query parameters.
        """
        if self._private_key is None:
            raise KalshiAPIError(
                401,
                "No private key loaded — cannot sign requests. "
                "Check the 'private_key_path' in config.yaml.",
            )
        path_clean = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_clean}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    # ------------------------------------------------------------------
    # Auth headers
    # ------------------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Return the three authentication headers required by Kalshi."""
        ts = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
        full_path = urlparse(self.base_url + path).path
        sig = self._sign(ts, method, full_path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    # ------------------------------------------------------------------
    # Core HTTP helpers with retry / rate-limit logic
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Enforce minimum interval between requests."""
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute an HTTP request with automatic retry on 429 and 5xx errors.
        """
        import json as _json

        url = f"{self.base_url}{path}"

        for attempt in range(1, self._MAX_RETRIES + 1):
            # Rebuild headers (including fresh timestamp) on every attempt.
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if authenticated:
                headers.update(self._auth_headers(method, path))

            self._throttle()
            self._last_request_ts = time.monotonic()

            try:
                resp = self._session.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=30
                )
            except requests.RequestException as exc:
                logger.warning("Network error (attempt %d/%d): %s", attempt, self._MAX_RETRIES, exc)
                if attempt == self._MAX_RETRIES:
                    raise KalshiAPIError(0, f"Network error after {self._MAX_RETRIES} retries: {exc}")
                time.sleep(self._RATE_LIMIT_BACKOFF_BASE * attempt)
                continue

            if resp.status_code == 429:
                backoff = self._RATE_LIMIT_BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate-limited (429). Backing off %.1fs …", backoff)
                time.sleep(backoff)
                continue

            if resp.status_code >= 500:
                backoff = self._RATE_LIMIT_BACKOFF_BASE * attempt
                logger.warning("Server error %d (attempt %d). Retrying in %.1fs …", resp.status_code, attempt, backoff)
                time.sleep(backoff)
                continue

            if resp.status_code == 401:
                # Detect clock-skew error and retry — Windows Time may need a moment to sync.
                try:
                    err_code = _json.loads(resp.text).get("error", {}).get("code", "")
                except Exception:
                    err_code = ""
                if err_code == "header_timestamp_expired" and attempt < self._MAX_RETRIES:
                    backoff = self._RATE_LIMIT_BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Clock skew detected (header_timestamp_expired). "
                        "Waiting %.1fs for time sync (attempt %d/%d) …",
                        backoff, attempt, self._MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    continue
                raise KalshiAPIError(resp.status_code, resp.text, resp)

            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text, resp)

            # 2xx — success
            if resp.status_code == 204:
                return {}
            return resp.json()

        # Should not reach here, but just in case:
        raise KalshiAPIError(0, "Exhausted retries without a successful response.")

    def _get(self, path: str, params: Optional[Dict] = None, authenticated: bool = True) -> Dict:
        return self._request("GET", path, params=params, authenticated=authenticated)

    def _post(self, path: str, body: Optional[Dict] = None) -> Dict:
        return self._request("POST", path, json_body=body)

    def _put(self, path: str, body: Optional[Dict] = None) -> Dict:
        return self._request("PUT", path, json_body=body)

    def _delete(self, path: str, params: Optional[Dict] = None) -> Dict:
        return self._request("DELETE", path, params=params)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    def _paginate(
        self,
        path: str,
        result_key: str,
        params: Optional[Dict] = None,
        limit: int = 200,
        max_pages: int = 50,
        authenticated: bool = True,
        return_meta: bool = False,
    ) -> Union[List[Dict], Tuple[List[Dict], Dict[str, Any]]]:
        """
        Automatically follow cursor-based pagination and collect all results.
        """
        params = dict(params or {})
        params["limit"] = limit
        collected: List[Dict] = []
        pages_fetched = 0
        truncated = False
        final_cursor = ""

        for _ in range(max_pages):
            data = self._get(path, params=params, authenticated=authenticated)
            pages_fetched += 1
            items = data.get(result_key, [])
            collected.extend(items)
            cursor = data.get("cursor", "")
            final_cursor = cursor or ""
            if not cursor or not items:
                break
            params["cursor"] = cursor
        else:
            # Loop exhausted (pagination cap hit) while cursor still available.
            if final_cursor:
                truncated = True
                logger.warning(
                    "Pagination cap reached for %s: collected %d items at %d pages; more data exists.",
                    path, len(collected), pages_fetched,
                )

        if return_meta:
            return collected, {
                "pages_fetched": pages_fetched,
                "truncated": truncated,
                "final_cursor": final_cursor,
                "collected": len(collected),
            }
        return collected

    # ==================================================================
    # PUBLIC API METHODS
    # ==================================================================

    # ---- Account / Portfolio -----------------------------------------

    def get_balance(self) -> Dict[str, Any]:
        """
        Retrieve the current account balance.

        Returns a dict with at least ``balance`` (int, in cents).
        """
        return self._get("/portfolio/balance")

    def get_positions(self, **kwargs) -> List[Dict]:
        """Return all open market positions (auto-paginated)."""
        params = {k: v for k, v in kwargs.items() if v is not None}
        return self._paginate("/portfolio/positions", "market_positions", params=params)

    def get_fills(self, limit: int = 200, **kwargs) -> List[Dict]:
        """Return trade fills / execution history (auto-paginated)."""
        params = {k: v for k, v in kwargs.items() if v is not None}
        return self._paginate("/portfolio/fills", "fills", params=params, limit=min(limit, 200))

    def get_orders(self, **kwargs) -> List[Dict]:
        """Return current resting orders (auto-paginated)."""
        params = {k: v for k, v in kwargs.items() if v is not None}
        return self._paginate("/portfolio/orders", "orders", params=params)

    def get_settlements(self, **kwargs) -> List[Dict]:
        """Return settlement history (auto-paginated)."""
        params = {k: v for k, v in kwargs.items() if v is not None}
        return self._paginate("/portfolio/settlements", "settlements", params=params)

    # ---- Orders ------------------------------------------------------

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
        time_in_force: Optional[str] = None,
        expiration_ts: Optional[int] = None,
        buy_max_cost: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Place a new order on a market using the Kalshi V2 events/orders endpoint.
        """
        cid = client_order_id or str(uuid.uuid4())
        price_cents = yes_price if yes_price is not None else (no_price if no_price is not None else 50)
        
        # In V2, event markets quote single-book bid/ask with fixed-point dollar prices.
        # Buying YES @ P cents -> bid @ P/100 dollars
        # Selling YES @ P cents -> ask @ P/100 dollars
        # Buying NO @ P cents -> ask @ (100 - P)/100 dollars
        # Selling NO @ P cents -> bid @ (100 - P)/100 dollars
        if str(side).lower() == "yes":
            effective_side = "bid" if str(action).lower() == "buy" else "ask"
            dollar_price = f"{max(1, min(99, int(price_cents))) / 100.0:.4f}"
        else:
            effective_side = "ask" if str(action).lower() == "buy" else "bid"
            yes_equiv_cents = 100 - max(1, min(99, int(price_cents)))
            dollar_price = f"{max(1, min(99, int(yes_equiv_cents))) / 100.0:.4f}"

        v2_body: Dict[str, Any] = {
            "ticker": ticker,
            "side": effective_side,
            "count": f"{count}.00" if isinstance(count, (int, float)) else str(count),
            "price": dollar_price,
            "time_in_force": time_in_force or "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": cid,
        }
        if expiration_ts:
            v2_body["expiration_time"] = int(expiration_ts)

        logger.info(
            "Creating order (V2): %s %s %s x%d @ %s (V2: %s %s) on %s",
            action, side, ticker, count,
            price_cents, effective_side, dollar_price, order_type,
        )

        try:
            res = self._post("/portfolio/events/orders", v2_body)
            if isinstance(res, dict) and "order_id" in res and "order" not in res:
                res = {"order": res}
            return res
        except KalshiAPIError as exc:
            if self.demo_mode:
                logger.info(
                    "[DEMO SIMULATION] Order simulated for %s (API error: %s). Recording simulated fill.",
                    ticker, exc,
                )
                return {
                    "order": {
                        "order_id": f"sim_{uuid.uuid4().hex[:12]}",
                        "ticker": ticker,
                        "side": side,
                        "action": action,
                        "count": count,
                        "price": price_cents,
                        "status": "executed",
                        "simulated": True,
                    }
                }
            raise

    def cancel_order(self, order_id: str) -> Dict:
        """Cancel a resting order by its server-assigned order ID."""
        logger.info("Cancelling order %s", order_id)
        return self._delete(f"/portfolio/orders/{order_id}")

    def amend_order(self, order_id: str, **kwargs) -> Dict:
        """Amend price or quantity of a resting order."""
        logger.info("Amending order %s: %s", order_id, kwargs)
        return self._post(f"/portfolio/orders/{order_id}/amend", kwargs)

    def get_order(self, order_id: str) -> Dict:
        """Retrieve a single order by ID."""
        return self._get(f"/portfolio/orders/{order_id}")

    # ---- Markets (public, no auth required) --------------------------

    def get_markets(
        self,
        status: Optional[str] = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 1000,
        max_pages: int = 50,
        return_meta: bool = False,
        **kwargs,
    ) -> Union[List[Dict], Tuple[List[Dict], Dict[str, Any]]]:
        """
        Retrieve markets with optional filters (auto-paginated).

        Common filters: ``status``, ``series_ticker``, ``event_ticker``,
        ``min_close_ts``, ``max_close_ts``.
        """
        params: Dict[str, Any] = {}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return self._paginate(
            "/markets",
            "markets",
            params=params,
            limit=min(limit, 1000),
            max_pages=max_pages,
            authenticated=False,
            return_meta=return_meta,
        )

    def get_market(self, ticker: str) -> Dict:
        """Retrieve a single market by ticker (unauthenticated)."""
        data = self._get(f"/markets/{ticker}", authenticated=False)
        return data.get("market", data)

    def get_market_orderbook(self, ticker: str) -> Dict:
        """Retrieve the current orderbook for a market (unauthenticated)."""
        data = self._get(f"/markets/{ticker}/orderbook", authenticated=False)

        # New Kalshi API format: orderbook_fp with dollar-string prices
        if "orderbook_fp" in data:
            fp = data["orderbook_fp"]

            def _parse_levels(raw):
                result = []
                for entry in (raw or []):
                    try:
                        price_cents = round(float(entry[0]) * 100)
                        qty = float(entry[1])
                        result.append([price_cents, qty])
                    except (ValueError, TypeError, IndexError):
                        pass
                return result

            return {
                "yes": _parse_levels(fp.get("yes_dollars") or []),
                "no": _parse_levels(fp.get("no_dollars") or []),
            }

        # Legacy format fallback
        return data.get("orderbook", data)

    def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 200,
        max_pages: int = 1,
    ) -> List[Dict]:
        """
        Retrieve recent public trades.

        Parameters
        ----------
        ticker : str, optional
            If provided, filter trades to a single market ticker.
            If omitted, returns recent trades across all markets.
        limit : int
            Page size (max 200).
        max_pages : int
            Number of cursor pages to fetch (default 1).
        """
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        return self._paginate(
            "/markets/trades",
            "trades",
            params=params,
            limit=min(limit, 200),
            max_pages=max_pages,
            authenticated=False,
        )

    def get_market_candlesticks(self, ticker: str, **kwargs) -> Dict:
        """Retrieve OHLC candlestick data for a market."""
        params = {"ticker": ticker}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return self._get(f"/markets/{ticker}/candlesticks", params=params, authenticated=False)

    # ---- Events & Series (public) ------------------------------------

    def get_events(self, **kwargs) -> List[Dict]:
        """List events (auto-paginated, unauthenticated)."""
        params = {k: v for k, v in kwargs.items() if v is not None}
        return self._paginate("/events", "events", params=params, authenticated=False)

    def get_event(self, event_ticker: str) -> Dict:
        """Retrieve a single event by ticker."""
        data = self._get(f"/events/{event_ticker}", authenticated=False)
        return data.get("event", data)

    def get_series(self, series_ticker: str) -> Dict:
        """Retrieve a single series by ticker."""
        data = self._get(f"/series/{series_ticker}", authenticated=False)
        return data.get("series", data)

    def get_series_list(self) -> List[Dict]:
        """List all available series."""
        data = self._get("/series", authenticated=False)
        return data.get("series", [])

    # ---- Exchange info -----------------------------------------------

    def get_exchange_status(self) -> Dict:
        """Check whether the exchange is currently open."""
        return self._get("/exchange/status", authenticated=False)

    def get_rate_limits(self) -> Dict:
        """
        Retrieve the authenticated user's rate-limit tier.

        Returns ``usage_tier``, ``read_limit``, ``write_limit``.
        """
        data = self._get("/account/limits")
        # Dynamically adjust internal throttle based on write limit.
        write_limit = data.get("write_limit", 10)
        self._min_request_interval = 1.0 / max(write_limit, 1)
        return data
