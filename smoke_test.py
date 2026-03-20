"""Offline smoke test for the rewritten bot."""
from __future__ import annotations

from assets import BTC
from sniper import Sniper


class DummyClient:
    def __init__(self) -> None:
        self.i = 0
        self.prices = [
            100.00,
            100.03,
            100.06,
            100.08,
            100.10,
            100.12,
            100.14,
            100.16,
            100.18,
            100.20,
            100.22,
        ]

    def get_balance(self) -> float:
        return 100.0

    def fetch_price(self, symbol: str) -> float:
        p = self.prices[min(self.i, len(self.prices) - 1)]
        self.i += 1
        return p

    def find_market(self, prefix: str, ts: int):
        return {"slug": f"{prefix}-updown-5m-{ts}", "condition_id": "cond", "up_token": "UPTOK", "down_token": "DNTOK"}

    def fetch_book(self, token_id: str):
        class B:
            best_bid = 0.64
            best_ask = 0.66
            spread = 0.02
            tick_size = "0.01"
            neg_risk = False

        return B()

    def get_buy_price(self, token_id: str, max_price: float, min_price: float) -> float:
        return 0.66

    def get_sell_price(self, token_id: str) -> float:
        return 0.75

    def submit_maker_buy(self, token_id: str, price: float, size: float, label: str):
        return "buy123"

    def submit_sell(self, token_id: str, price: float, size: float, label: str):
        return "sell123"

    def get_market_resolution(self, slug: str):
        return None

    def fetch_midpoint(self, token_id: str) -> float:
        return 0.71


if __name__ == "__main__":
    client = DummyClient()
    sniper = Sniper(BTC, client, dry_run=True, mode="safe", max_bet=20)
    base = 1_700_000_000
    window = base - (base % 300)

    for ts in [window + 240, window + 242, window + 244, window + 246, window + 248, window + 250, window + 252, window + 254]:
        sniper.step(ts)
    sniper.step(window + 301)

    print(sniper.summary())


    assert sniper.stats.fired == 1
    assert sniper.stats.closed == 1
    assert sniper.stats.early_exits == 1
    assert sniper.stats.wins == 1
    assert sniper.stats.losses == 0
    assert sniper.stats.flats == 0
