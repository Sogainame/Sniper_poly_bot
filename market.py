"""Polymarket API layer.

Fixes applied:
- does not misuse `/price` or `/prices` with exchange tickers; those endpoints require token IDs and side
- parses `/midpoint` using `mid_price`
- uses immediate execution logic (FOK market orders) for sniper entry/exit
- exposes stable helper methods used by the rest of the bot
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
        ApiCreds, AssetType, BalanceAllowanceParams, OrderType,
        MarketOrderArgs, PartialCreateOrderOptions,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:  # pragma: no cover - optional dependency in this environment
    ClobClient = None  # type: ignore[assignment]
    ApiCreds = None  # type: ignore[assignment]
    AssetType = None  # type: ignore[assignment]
    BalanceAllowanceParams = None  # type: ignore[assignment]
    MarketOrderArgs = None  # type: ignore[assignment]
    PartialCreateOrderOptions = None  # type: ignore[assignment]
    BUY = "BUY"
    SELL = "SELL"

    class OrderType:  # type: ignore[no-redef]
        FOK = "FOK"
        FAK = "FAK"
        GTC = "GTC"


@dataclass
class Book:
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    tick_size: str = "0.01"
    neg_risk: bool = False


class PolymarketClient:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=config.HTTP_TIMEOUT)
        self.clob = self._init_clob()

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _call_any(obj: Any, *names: str, **kwargs: Any) -> Any:
        for name in names:
            fn = getattr(obj, name, None)
            if callable(fn):
                return fn(**kwargs) if kwargs else fn()
        raise AttributeError(f"Missing method variants: {', '.join(names)}")

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
            if ApiCreds and config.POLY_API_KEY and config.POLY_API_SECRET and config.POLY_API_PASSPHRASE:
                kwargs["creds"] = ApiCreds(
                    api_key=config.POLY_API_KEY,
                    api_secret=config.POLY_API_SECRET,
                    api_passphrase=config.POLY_API_PASSPHRASE,
                )
                return ClobClient(**kwargs)

            client = ClobClient(**kwargs)
            creds = getattr(client, "create_or_derive_api_creds", None)
            if callable(creds):
                derived = client.create_or_derive_api_creds()
                setter = getattr(client, "set_api_creds", None)
                if callable(setter) and derived is not None:
                    setter(derived)
            return client
        except Exception as exc:
            print(f"[!] CLOB init warning: {exc}")
            return None

    def get_balance(self) -> float | None:
        if self.clob is None or BalanceAllowanceParams is None or AssetType is None:
            return None
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self._call_any(self.clob, "get_balance_allowance", "getBalanceAllowance", params=params)
            raw = self._to_float(resp.get("balance", 0) if isinstance(resp, dict) else getattr(resp, "balance", 0))
            return raw / 1e6 if raw > 10_000 else raw
        except Exception:
            return None

    def fetch_price(self, binance_symbol: str) -> float:
        try:
            resp = self.http.get(BINANCE_TICKER, params={"symbol": binance_symbol}, timeout=5.0)
            if resp.status_code == 200:
                return self._to_float(resp.json().get("price", 0))
        except Exception:
            pass
        return 0.0

    def fetch_midpoint(self, token_id: str) -> float:
        try:
            resp = self.http.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                mid = self._to_float(data.get("mid_price", 0))
                if mid > 0:
                    return mid
                return self._to_float(data.get("mid", 0))
        except Exception:
            pass
        return 0.0

    def fetch_last_trade(self, token_id: str) -> dict[str, Any]:
        try:
            resp = self.http.get(f"{CLOB_HOST}/last-trade-price", params={"token_id": token_id}, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "price": self._to_float(data.get("price", 0)),
                    "side": str(data.get("side", "") or ""),
                }
        except Exception:
            pass
        return {"price": 0.0, "side": ""}

    def fetch_best_price(self, token_id: str, side: str) -> float:
        try:
            resp = self.http.get(
                f"{CLOB_HOST}/price",
                params={"token_id": token_id, "side": side},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return self._to_float(data.get("price", 0))
        except Exception:
            pass
        return 0.0

    def fetch_book(self, token_id: str) -> Book:
        bid = self.fetch_best_price(token_id, "BUY")   # best bid = highest buy offer
        ask = self.fetch_best_price(token_id, "SELL")   # best ask = lowest sell offer

        if bid <= 0 or ask <= 0:
            return Book()

        return Book(
            best_bid=bid,
            best_ask=ask,
            spread=max(ask - bid, 0.0),
            tick_size="0.01",
            neg_risk=False,
        )

    def get_buy_price(self, token_id: str, max_price: float, min_price: float) -> float:
        book = self.fetch_book(token_id)
        ask = round(book.best_ask, 4)
        if ask <= 0 or ask > max_price or ask < min_price:
            return 0.0
        return ask

    def get_sell_price(self, token_id: str) -> float:
        book = self.fetch_book(token_id)
        bid = round(book.best_bid, 4)
        return bid if bid > 0 else 0.0

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
            "raw": market,
        }

    def get_market_resolution(self, slug: str) -> str | None:
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

    def _market_order_options(self, token_id: str) -> dict[str, Any]:
        book = self.fetch_book(token_id)
        return {"tick_size": book.tick_size or "0.01", "neg_risk": book.neg_risk}

    def _submit_market_order(self, token_id: str, side: Any, amount: float, price: float, label: str) -> str | None:
        if self.clob is None:
            print(f"[!] Live order blocked for {label}: py-clob-client/client not available")
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=round(amount, 2),
                    price=round(price, 2),
                    side=side,
                )
                options = None
                if PartialCreateOrderOptions is not None:
                    options = PartialCreateOrderOptions(
                        tick_size="0.01",
                        neg_risk=False,
                    )

                signed = self.clob.create_market_order(order_args, options)
                resp = self.clob.post_order(signed, OrderType.FOK)

                if isinstance(resp, dict):
                    oid = resp.get("orderID") or resp.get("id")
                else:
                    oid = getattr(resp, "orderID", None) or getattr(resp, "id", None)

                if oid:
                    print(f"[✓] {label} order={oid}")
                return oid
            except Exception as exc:
                err = str(exc)
                print(f"[!] {label} attempt {attempt}: {err}")
                if "NOT_ENOUGH" in err.upper() or "INSUFFICIENT" in err.upper():
                    send_telegram(f"⚠️ Low balance/allowance for {label}")
                    return None
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def submit_maker_buy(self, token_id: str, price: float, size: float, label: str) -> str | None:
        spend = round(price * size, 4)
        if spend <= 0:
            return None
        return self._submit_market_order(token_id, BUY, spend, round(price, 4), f"BUY {label}")

    def submit_sell(self, token_id: str, price: float, size: float, label: str) -> str | None:
        shares = round(size, 4)
        if shares <= 0:
            return None
        return self._submit_market_order(token_id, SELL, shares, round(price, 4), f"SELL {label}")
