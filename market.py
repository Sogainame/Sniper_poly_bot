"""
Polymarket API layer — market discovery, prices, order submission.

Balance fix based on py-clob-client issue #83:
  get_balance_allowance() crashes when params=None.
  Workaround: pass BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
  
Creds: create_or_derive_api_creds() returns ApiCreds object (not dict),
  store attrs manually for REST fallback.
"""

import json
import os
import time

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import config
from notifier import send_telegram

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
CHAIN_ID = 137

MAX_ORDER_RETRIES = 2
RETRY_DELAY = 0.5
MAKER_SPREAD = 0.02


class PolymarketClient:

    def __init__(self):
        self.http = httpx.Client(timeout=10.0)
        self._api_key = ""
        self._api_secret = ""
        self._api_passphrase = ""
        self.clob = self._init_clob()

    def _init_clob(self) -> ClobClient:
        client = ClobClient(
            host=CLOB_HOST,
            key=config.POLY_PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=1,
            funder=config.POLY_FUNDER_ADDRESS,
        )
        try:
            creds = client.create_or_derive_api_creds()
            if creds:
                client.set_api_creds(creds)
                # Store creds for direct REST (ApiCreds is not a dict)
                self._api_key = getattr(creds, 'api_key', '')
                self._api_secret = getattr(creds, 'api_secret', '')
                self._api_passphrase = getattr(creds, 'api_passphrase', '')
        except Exception as e:
            print(f"  [!] CLOB creds warning: {e}")
        return client

    # ── Balance ───────────────────────────────────────────────────────────
    # py-clob-client bug #83: get_balance_allowance() without params crashes.
    # Fix: pass BalanceAllowanceParams explicitly.
    # Fallback: direct REST with stored API creds.

    def get_balance(self) -> float | None:
        # Method 1: CLOB client with explicit params (fix for bug #83)
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.clob.get_balance_allowance(params)
            if isinstance(resp, dict):
                raw = float(resp.get("balance", 0) or 0)
                bal = raw / 1e6 if raw > 10_000 else raw
                if bal > 0.01:
                    return bal
        except Exception:
            pass

        # Method 2: Direct CLOB REST API
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
                        raw = float(data.get("balance", 0) or 0)
                        bal = raw / 1e6 if raw > 10_000 else raw
                        if bal > 0.01:
                            return bal
            except Exception:
                pass

        return None

    # ── Binance Price ─────────────────────────────────────────────────────

    def fetch_price(self, binance_symbol: str) -> float:
        try:
            r = self.http.get(BINANCE_TICKER,
                              params={"symbol": binance_symbol}, timeout=5.0)
            if r.status_code == 200:
                return float(r.json().get("price", 0))
        except Exception:
            pass
        return 0.0

    # ── Polymarket Token Prices ───────────────────────────────────────────

    def fetch_midpoint(self, token_id: str) -> float:
        try:
            r = self.http.get(f"{CLOB_HOST}/midpoint",
                              params={"token_id": token_id}, timeout=5.0)
            if r.status_code == 200:
                mid = float(r.json().get("mid", 0))
                if mid > 0.01:
                    return mid
        except Exception:
            pass
        return 0.0

    def fetch_book(self, token_id: str) -> dict:
        try:
            r = self.http.get(f"{CLOB_HOST}/book",
                              params={"token_id": token_id}, timeout=5.0)
            if r.status_code == 200:
                book = r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                return {
                    "best_bid": float(bids[0]["price"]) if bids else 0.0,
                    "best_ask": float(asks[0]["price"]) if asks else 0.0,
                }
        except Exception:
            pass
        return {"best_bid": 0.0, "best_ask": 0.0}

    def get_buy_price(self, token_id: str, max_price: float, min_price: float) -> float:
        mid = self.fetch_midpoint(token_id)
        book = self.fetch_book(token_id)
        if mid > 0.01:
            price = mid + MAKER_SPREAD
        elif book["best_ask"] > 0:
            price = book["best_ask"] - 0.01
        else:
            return 0.0
        price = round(min(price, max_price), 2)
        return price if price >= min_price else 0.0

    # ── Market Lookup ─────────────────────────────────────────────────────

    def find_market(self, slug_prefix: str, window_ts: int) -> dict | None:
        slug = f"{slug_prefix}-updown-5m-{window_ts}"
        try:
            resp = self.http.get(f"{GAMMA_API}/markets",
                                 params={"slug": slug}, timeout=10.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            markets = data if isinstance(data, list) else [data]
            for m in markets:
                if not m or m.get("slug") != slug:
                    continue
                clob_ids = m.get("clobTokenIds", "")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                if len(clob_ids) < 2:
                    continue
                return {
                    "slug": slug,
                    "condition_id": m.get("conditionId", ""),
                    "token_ids": clob_ids,
                }
        except Exception as e:
            print(f"  [!] Market lookup: {e}")
        return None

    # ── Order Submission ──────────────────────────────────────────────────

    def submit_maker_buy(self, token_id: str, price: float, size: float,
                         label: str) -> str | None:
        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                args = OrderArgs(token_id=token_id, price=round(price, 2),
                                 size=round(size, 1), side=BUY)
                signed = self.clob.create_order(args)
                resp = self.clob.post_order(signed, OrderType.GTC)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"  [ORDER] MAKER BUY {label} @ {price:.2f} x {size:.0f}sh"
                      f" | ID: {oid or '?'}")
                return oid
            except Exception as e:
                err = str(e).lower()
                print(f"  [!] Order attempt {attempt}: {e}")
                if any(kw in err for kw in ("not enough", "balance", "insufficient")):
                    send_telegram(f"⚠️ Low balance for {label}!")
                    return None
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def submit_sell(self, token_id: str, price: float, size: float,
                    label: str) -> str | None:
        """
        Sell winning tokens back to USDC.
        After a win, tokens are worth ~$1.00. Sell at $0.99 to
        convert back to cash instantly (~$0.01/share fee).
        Based on gengar_polymarket_bot auto-claim approach.
        """
        from py_clob_client.order_builder.constants import SELL
        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                args = OrderArgs(token_id=token_id, price=round(price, 2),
                                 size=round(size, 1), side=SELL)
                signed = self.clob.create_order(args)
                resp = self.clob.post_order(signed, OrderType.GTC)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"  [SELL] {label} @ {price:.2f} x {size:.0f}sh"
                      f" | ID: {oid or '?'}")
                return oid
            except Exception as e:
                print(f"  [!] Sell attempt {attempt}: {e}")
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None
