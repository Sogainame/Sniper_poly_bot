"""Polymarket API layer — market discovery, prices, and order submission.

This version fixes the critical execution/data issues in the original file:
- stops misusing /prices with a Binance ticker as if it were a Polymarket token_id
- treats sniper entry/exit as immediate taker execution, not resting GTC quotes
- uses market-order semantics from the official py-clob-client for FOK orders
- keeps the public method names stable so the rest of the bot does not break
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

import config
from notifier import send_telegram

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
CHAIN_ID = 137
MAX_ORDER_RETRIES = 2
RETRY_DELAY = 0.5


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
                self._api_key = getattr(creds, "api_key", "")
                self._api_secret = getattr(creds, "api_secret", "")
                self._api_passphrase = getattr(creds, "api_passphrase", "")
        except Exception as e:
            print(f" [!] CLOB creds warning: {e}")
        return client

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _extract_order_id(resp: Any) -> str | None:
        if isinstance(resp, dict):
            return resp.get("orderID") or resp.get("id")
        return getattr(resp, "orderID", None) or getattr(resp, "id", None)

    # ── Balance ───────────────────────────────────────────────────────────
    def get_balance(self) -> float | None:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.clob.get_balance_allowance(params)
            if isinstance(resp, dict):
                raw = self._to_float(resp.get("balance", 0) or 0)
                bal = raw / 1e6 if raw > 10_000 else raw
                if bal > 0.01:
                    return bal
        except Exception:
            pass

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
                        raw = self._to_float(data.get("balance", 0) or 0)
                        bal = raw / 1e6 if raw > 10_000 else raw
                        if bal > 0.01:
                            return bal
            except Exception:
                pass
        return None

    # ── Reference Price Feed ──────────────────────────────────────────────
    def fetch_price(self, binance_symbol: str) -> float:
        """Get the reference spot proxy used by the signal engine.

        Important: this is NOT a Polymarket token endpoint.
        The original code incorrectly called /prices with token_id=btcusdt.
        /prices expects Polymarket token IDs, not exchange tickers.

        For now this method uses Binance REST as an explicit spot proxy.
        The execution/settlement logic should not assume this is the exact
        Polymarket resolution source.
        """
        try:
            r = self.http.get(BINANCE_TICKER, params={"symbol": binance_symbol}, timeout=5.0)
            if r.status_code == 200:
                return self._to_float(r.json().get("price", 0))
        except Exception:
            pass
        return 0.0

    # ── Polymarket Token Prices ───────────────────────────────────────────
    def fetch_midpoint(self, token_id: str) -> float:
        try:
            r = self.http.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                mid = self._to_float(data.get("mid", 0))
                if mid > 0.0:
                    return mid
        except Exception:
            pass
        return 0.0

    def fetch_book(self, token_id: str) -> dict:
        try:
            r = self.http.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=5.0)
            if r.status_code == 200:
                book = r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                best_bid = self._to_float(bids[0].get("price")) if bids else 0.0
                best_ask = self._to_float(asks[0].get("price")) if asks else 0.0
                return {"best_bid": best_bid, "best_ask": best_ask}
        except Exception:
            pass
        return {"best_bid": 0.0, "best_ask": 0.0}

    def get_buy_price(self, token_id: str, max_price: float, min_price: float) -> float:
        """Executable entry price for an immediate buy.

        Uses best ask only. If there is no ask, do not synthesize a fake fill.
        """
        book = self.fetch_book(token_id)
        ask = round(book["best_ask"], 2) if book["best_ask"] > 0 else 0.0
        if ask <= 0:
            return 0.0
        if ask > max_price or ask < min_price:
            return 0.0
        return ask

    def get_sell_price(self, token_id: str) -> float:
        """Executable exit price for an immediate sell (best bid)."""
        book = self.fetch_book(token_id)
        bid = round(book["best_bid"], 2) if book["best_bid"] > 0 else 0.0
        return bid if bid > 0 else 0.0

    # ── Market Lookup ─────────────────────────────────────────────────────
    def find_market(self, slug_prefix: str, window_ts: int) -> dict | None:
        slug = f"{slug_prefix}-updown-5m-{window_ts}"
        try:
            resp = self.http.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10.0)
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

                outcomes = m.get("outcomes", "")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = []

                up_token = clob_ids[0]
                down_token = clob_ids[1]
                if len(outcomes) >= 2:
                    for i, name in enumerate(outcomes):
                        lname = str(name).lower()
                        if lname in ("up", "yes") and i < len(clob_ids):
                            up_token = clob_ids[i]
                        elif lname in ("down", "no") and i < len(clob_ids):
                            down_token = clob_ids[i]

                return {
                    "slug": slug,
                    "condition_id": m.get("conditionId", ""),
                    "up_token": up_token,
                    "down_token": down_token,
                }
        except Exception as e:
            print(f" [!] Market lookup: {e}")
        return None

    # ── Order Submission ──────────────────────────────────────────────────
    def submit_maker_buy(self, token_id: str, price: float, size: float, label: str) -> str | None:
        """Compatibility shim for the old method name.

        This is intentionally NO LONGER a resting maker order.
        For a sniper strategy, entry must either fill immediately or fail.
        Uses FOK so bot state does not assume a partial position that never filled.
        """
        spend_usdc = round(price * size, 2)
        if spend_usdc <= 0:
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=spend_usdc,
                    side=BUY,
                    price=round(price, 2),
                    order_type=OrderType.FOK,
                )
                signed = self.clob.create_market_order(mo)
                resp = self.clob.post_order(signed, OrderType.FOK)
                oid = self._extract_order_id(resp)
                print(
                    f" [ORDER] TAKER BUY {label} @ <= {price:.2f}"
                    f" | spend=${spend_usdc:.2f} | ID: {oid or '?'}"
                )
                return oid
            except Exception as e:
                err = str(e).lower()
                print(f" [!] Buy attempt {attempt}: {e}")
                if any(kw in err for kw in ("not enough", "balance", "insufficient")):
                    send_telegram(f"⚠️ Low balance for {label}!")
                    return None
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def submit_sell(self, token_id: str, price: float, size: float, label: str) -> str | None:
        """Immediate sell using FOK.

        For SELL market orders, amount is the number of shares.
        """
        if size <= 0:
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=round(size, 4),
                    side=SELL,
                    price=round(price, 2),
                    order_type=OrderType.FOK,
                )
                signed = self.clob.create_market_order(mo)
                resp = self.clob.post_order(signed, OrderType.FOK)
                oid = self._extract_order_id(resp)
                print(
                    f" [SELL] TAKER {label} @ >= {price:.2f}"
                    f" x {size:.2f}sh | ID: {oid or '?'}"
                )
                return oid
            except Exception as e:
                print(f" [!] Sell attempt {attempt}: {e}")
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None
