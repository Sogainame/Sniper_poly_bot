"""Allowance helper.

Polymarket approval is an onchain token approval, not a CLOB-only setting.
This script checks whether the SDK is available and reminds the user to approve in
Polymarket UI or via a dedicated onchain approval tool.
"""
from __future__ import annotations

from market import PolymarketClient


if __name__ == "__main__":
    client = PolymarketClient()
    balance = client.get_balance()
    print("Polymarket approvals are onchain allowances.")
    print("Use the Polymarket UI on first trade, or your own approval script against the exchange/CTF contracts.")
    print(f"Current collateral balance: {'n/a' if balance is None else f'${balance:.2f}'}")
