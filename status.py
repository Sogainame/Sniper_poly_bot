"""Minimal runtime status helper."""
from __future__ import annotations

from assets import BTC
from market import PolymarketClient


if __name__ == "__main__":
    client = PolymarketClient()
    bal = client.get_balance()
    ref = client.fetch_price(BTC.binance_symbol)
    print(f"Balance: {'n/a' if bal is None else f'${bal:.2f}'}")
    print(f"Reference price ({BTC.binance_symbol}): {ref or 'n/a'}")
