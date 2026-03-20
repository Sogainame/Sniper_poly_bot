"""Simple balance check."""
from __future__ import annotations

from market import PolymarketClient


if __name__ == "__main__":
    client = PolymarketClient()
    bal = client.get_balance()
    if bal is None:
        print("Balance unavailable. Check py-clob-client, API creds, signature type, and funder address.")
    else:
        print(f"Balance: ${bal:.2f}")
