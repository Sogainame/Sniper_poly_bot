"""Polymarket API layer.

V3: V2 architecture (typed Book, _load_market, get_market_resolution)
    + V1 execution (GTC maker orders via OrderArgs, auto-sell at $0.99)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

import config
from notifier import send_telegram

CLOB_HOST = config.CLOB_HOST
GAMMA_API = config.GAMMA_API
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
MAX_ORDER_RETRIES = 2
RETRY_DELAY = 0.5

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams,
        OrderArgs, OrderType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:
    ClobClient = None  # type: ignore[assignment]
    ApiCreds = None  # type: ignore[assignment]
    AssetType = None  # type: ignore[assignment]
    BalanceAllowanceParams = None  # type: ignore[assignment]
    OrderArgs = None  # type: ignore[assignment]
    BUY = "BUY"
    SELL = "SELL"

    class OrderType:  # type: ignore[no-redef]
        GTC = "GTC"
        FOK = "FOK"


# ── V2: Typed Book ────────────────────────────────────────────────────────────

@dataclass
class Book:
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0


class PolymarketClient:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=config.HTTP_TIMEOUT)
        self._api_key = ""
        self._api_secret = ""
        self._api_passphrase = ""
        self.clob = self._init_clob()

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    # ── CLOB Init (V2 style with V1 cred storage) ─────────────────────────

    def _init_clob(self) -> Any | None:
        if ClobClient is None or not config.POLY_PRIVATE_KEY:
            return None
        try:
            kwargs: dict[str, Any] = {
                "host": CLOB_HOST,
                "chain_id": config.CHAIN_ID,
                "key": config.POLY_PRIVATE_KEY,
                "signature_type": config.POLY_SIGNATURE_TYPE,
            }
            if config.POLY_FUNDER_ADDRESS:
                kwargs["funder"] = config.POLY_FUNDER_ADDRESS

            # Use pre-configured API creds if available
            if ApiCreds and config.POLY_API_KEY and config.POLY_API_SECRET and config.POLY_API_PASSPHRASE:
                kwargs["creds"] = ApiCreds(
                    api_key=config.POLY_API_KEY,
                    api_secret=config.POLY_API_SECRET,
                    api_passphrase=config.POLY_API_PASSPHRASE,
                )
                self._api_key = config.POLY_API_KEY
                self._api_secret = config.POLY_API_SECRET
                self._api_passphrase = config.POLY_API_PASSPHRASE
                return ClobClient(**kwargs)

            # Derive creds (V1 approach)
            client = ClobClient(**kwargs)
            try:
                creds = client.create_or_derive_api_creds()
                if creds:
                    client.set_api_creds(creds)
                    self._api_key = getattr(creds, "api_key", "")
                    self._api_secret = getattr(creds, "api_secret", "")
                    self._api_passphrase = getattr(creds, "api_passphrase", "")
            except Exception as e:
                print(f"[!] CLOB creds warning: {e}")
            return client
        except Exception as exc:
            print(f"[!] CLOB init warning: {exc}")
            return None

    # ── Balance (V1 with fallback to REST) ─────────────────────────────────

    def get_balance(self) -> float | None:
        if self.clob is None or BalanceAllowanceParams is None or AssetType is None:
            return None
        # Method 1: CLOB client
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.clob.get_balance_allowance(params)
            if isinstance(resp, dict):
                raw = self._to_float(resp.get("balance", 0))
                bal = raw / 1e6 if raw > 10_000 else raw
                if bal > 0.01:
                    return bal
        except Exception:
            pass

        # Method 2: Direct REST (V1 fallback)
        if self._api_key:
            try:
                headers = {
                    "POLY_API_KEY": self._api_key,
                    "POLY_API_SECRET": self._api_secret,
                    "POLY_PASSPHRASE": self._api_passphrase,
                }
                r = self.http.get(
                    f"{CLOB_HOST}/balance-allowance",
                    params={"asset_type": "COLLATERAL", "signature_type": "1"},
                    headers=headers,
                    timeout=10.0,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        raw = self._to_float(data.get("balance", 0))
                        bal = raw / 1e6 if raw > 10_000 else raw
                        if bal > 0.01:
                            return bal
            except Exception:
                pass
        return None

    # ── Price Feed (Binance REST fallback — primary is WS in price_feed.py) ──

    def fetch_price(self, binance_symbol: str) -> float:
        try:
            resp = self.http.get(BINANCE_TICKER, params={"symbol": binance_symbol}, timeout=5.0)
            if resp.status_code == 200:
                return self._to_float(resp.json().get("price", 0))
        except Exception:
            pass
        return 0.0

    # ── Token Prices (V2 typed Book) ───────────────────────────────────────

    def fetch_midpoint(self, token_id: str) -> float:
        try:
            resp = self.http.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                mid = self._to_float(data.get("mid", 0))
                if mid > 0.01:
                    return mid
                mid = self._to_float(data.get("mid_price", 0))
                if mid > 0.01:
                    return mid
        except Exception:
            pass
        return 0.0

    def _fetch_best_price(self, token_id: str, side: str) -> float:
        """Fetch best price via /price endpoint (returns actual best bid/ask)."""
        try:
            resp = self.http.get(
                f"{CLOB_HOST}/price",
                params={"token_id": token_id, "side": side},
                timeout=5.0,
            )
            if resp.status_code == 200:
                return self._to_float(resp.json().get("price", 0))
        except Exception:
            pass
        return 0.0

    def fetch_book(self, token_id: str) -> Book:
        """Fetch best bid/ask via /price endpoint.

        Note: /book endpoint returns bids sorted lowest-first (0.01, 0.02, ...),
        NOT best-first. The /price endpoint returns the actual best price directly.
        """
        bid = self._fetch_best_price(token_id, "SELL")   # best bid = highest buy offer
        ask = self._fetch_best_price(token_id, "BUY")    # best ask = lowest sell offer

        if bid <= 0 or ask <= 0:
            return Book()

        return Book(
            best_bid=bid,
            best_ask=ask,
            spread=max(ask - bid, 0.0),
        )

    def get_buy_price(self, token_id: str, max_price: float, min_price: float) -> float:
        """Buy price = best_ask (V1: guaranteed fill, taker fee ~1.5%)."""
        book = self.fetch_book(token_id)
        if book.best_ask <= 0:
            mid = self.fetch_midpoint(token_id)
            if mid > 0.01:
                price = mid + 0.02
            else:
                return 0.0
        else:
            price = book.best_ask
        price = round(min(price, max_price), 2)
        return price if price >= min_price else 0.0

    def get_sell_price(self, token_id: str) -> float:
        """Sell price = best_bid (V1: guaranteed fill)."""
        book = self.fetch_book(token_id)
        if book.best_bid > 0:
            return round(book.best_bid, 2)
        mid = self.fetch_midpoint(token_id)
        if mid > 0.01:
            return round(mid - 0.01, 2)
        return 0.0

    # ── Market Lookup (V2 clean version) ───────────────────────────────────

    def _load_market(self, slug: str) -> dict[str, Any] | None:
        try:
            resp = self.http.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10.0)
            if resp.status_code != 200:
                return None
            payload = resp.json()
            markets = payload if isinstance(payload, list) else [payload]
            return next((m for m in markets if m and m.get("slug") == slug), None)
        except Exception:
            return None

    def find_market(self, slug_prefix: str, window_ts: int) -> dict[str, Any] | None:
        slug = f"{slug_prefix}-updown-5m-{window_ts}"
        market = self._load_market(slug)
        if not market:
            return None

        clob_ids = market.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                clob_ids = []
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        up_token = clob_ids[0] if len(clob_ids) >= 1 else ""
        down_token = clob_ids[1] if len(clob_ids) >= 2 else ""
        for i, name in enumerate(outcomes):
            if i >= len(clob_ids):
                break
            lname = str(name).lower()
            if lname in {"up", "yes"}:
                up_token = clob_ids[i]
            elif lname in {"down", "no"}:
                down_token = clob_ids[i]

        return {
            "slug": slug,
            "condition_id": market.get("conditionId", ""),
            "up_token": up_token,
            "down_token": down_token,
        }

    def get_market_resolution(self, slug: str) -> str | None:
        """V2: Check Gamma API for market resolution winner."""
        market = self._load_market(slug)
        if not market:
            return None

        tokens = market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []
        for token in tokens:
            if bool(token.get("winner", False)):
                outcome = str(token.get("outcome", "")).lower()
                if outcome in {"yes", "up"}:
                    return "UP"
                if outcome in {"no", "down"}:
                    return "DOWN"

        winner = str(market.get("winningOutcome", "") or market.get("winner", "")).lower()
        if winner in {"yes", "up"}:
            return "UP"
        if winner in {"no", "down"}:
            return "DOWN"
        return None

    # ── V1: GTC Maker Orders ──────────────────────────────────────────────
    # Key difference from V2: uses OrderArgs + GTC (limit orders that fill)
    # instead of MarketOrderArgs + FOK (which never filled at edge ≥3%)

    def submit_maker_buy(self, token_id: str, price: float, size: float,
                         label: str) -> str | None:
        if self.clob is None or OrderArgs is None:
            print(f"[!] Live order blocked for {label}: CLOB client not available")
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 2),
                    size=round(size, 1),
                    side=BUY,
                    fee_rate_bps=0,  # maker = 0 fee (required on fee-enabled markets)
                )
                signed = self.clob.create_order(args)
                resp = self.clob.post_order(signed, OrderType.GTC, post_only=True)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"[ORDER] MAKER BUY {label} @ {price:.2f} x {size:.0f}sh | ID: {oid or '?'}")
                return oid
            except Exception as e:
                err = str(e).lower()
                print(f"[!] Order attempt {attempt}: {e}")
                if any(kw in err for kw in ("not enough", "balance", "insufficient")):
                    send_telegram(f"⚠️ Low balance for {label}!")
                    return None
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def submit_sell(self, token_id: str, price: float, size: float,
                    label: str) -> str | None:
        """V1: Sell tokens back to USDC via GTC order.

        After a win, tokens worth ~$1.00. Sell at $0.99 to convert back
        to cash (~$0.01/share fee). Based on gengar_polymarket_bot approach.
        """
        if self.clob is None or OrderArgs is None:
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 2),
                    size=round(size, 1),
                    side=SELL,
                    fee_rate_bps=0,  # maker = 0 fee
                )
                signed = self.clob.create_order(args)
                resp = self.clob.post_order(signed, OrderType.GTC, post_only=True)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"[SELL] {label} @ {price:.2f} x {size:.0f}sh | ID: {oid or '?'}")
                return oid
            except Exception as e:
                print(f"[!] Sell attempt {attempt}: {e}")
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def update_balance_allowance(self, token_id: str) -> None:
        """V1: Refresh balance allowance for conditional token before selling."""
        if self.clob is None:
            return
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            self.clob.update_balance_allowance(params)
        except Exception:
            pass

    def get_token_balance(self, token_id: str) -> float:
        """Get real conditional token balance (in shares, not raw units)."""
        if self.clob is None or BalanceAllowanceParams is None or AssetType is None:
            return 0.0
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            resp = self.clob.get_balance_allowance(params)
            if isinstance(resp, dict):
                raw = self._to_float(resp.get("balance", 0))
                return raw / 1e6 if raw > 10_000 else raw
        except Exception:
            pass
        return 0.0

    def get_order_status(self, order_id: str) -> str:
        """Poll order fill status. Returns: LIVE, MATCHED, FILLED, CANCELLED, or ''."""
        if self.clob is None or not order_id:
            return ""
        try:
            resp = self.clob.get_order(order_id)
            if isinstance(resp, dict):
                return str(resp.get("status", resp.get("order_status", ""))).upper()
        except Exception:
            pass
        return ""
