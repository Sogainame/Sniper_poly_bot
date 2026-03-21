"""Per-asset configuration for short-horizon Polymarket markets."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    name: str
    slug_prefix: str
    binance_symbol: str
    chainlink_symbol: str
    min_delta_pct: float
    min_confidence: float
    max_token_price: float
    min_token_price: float
    eval_start_secs: int
    eval_end_secs: int
    priority: int
    confirm_ticks: int = 3
    early_exit_profit: float = 0.08
    max_spread: float = 0.06
    min_score_gap: float = 0.40
    enabled: bool = True


BTC = AssetConfig(
    name="BTC",
    slug_prefix="btc",
    binance_symbol="BTCUSDT",
    chainlink_symbol="btc/usd",
    min_delta_pct=0.015,
    min_confidence=0.30,
    max_token_price=0.90,
    min_token_price=0.35,
    eval_start_secs=120,
    eval_end_secs=20,
    priority=1,
    confirm_ticks=2,
    early_exit_profit=0.06,
    max_spread=0.06,
    min_score_gap=0.20,
)

SOL = AssetConfig(
    name="SOL",
    slug_prefix="sol",
    binance_symbol="SOLUSDT",
    chainlink_symbol="sol/usd",
    min_delta_pct=0.03,
    min_confidence=0.40,
    max_token_price=0.80,
    min_token_price=0.52,
    eval_start_secs=150,
    eval_end_secs=30,
    priority=2,
    confirm_ticks=3,
    early_exit_profit=0.08,
    max_spread=0.05,
    min_score_gap=0.55,
)

ETH = AssetConfig(
    name="ETH",
    slug_prefix="eth",
    binance_symbol="ETHUSDT",
    chainlink_symbol="eth/usd",
    min_delta_pct=0.04,
    min_confidence=0.45,
    max_token_price=0.78,
    min_token_price=0.53,
    eval_start_secs=160,
    eval_end_secs=30,
    priority=3,
    confirm_ticks=4,
    early_exit_profit=0.08,
    max_spread=0.05,
    min_score_gap=0.60,
)

XRP = AssetConfig(
    name="XRP",
    slug_prefix="xrp",
    binance_symbol="XRPUSDT",
    chainlink_symbol="xrp/usd",
    min_delta_pct=0.05,
    min_confidence=0.50,
    max_token_price=0.76,
    min_token_price=0.53,
    eval_start_secs=160,
    eval_end_secs=30,
    priority=4,
    confirm_ticks=4,
    early_exit_profit=0.08,
    max_spread=0.05,
    min_score_gap=0.60,
)

DOGE = AssetConfig(
    name="DOGE",
    slug_prefix="doge",
    binance_symbol="DOGEUSDT",
    chainlink_symbol="doge/usd",
    min_delta_pct=0.08,
    min_confidence=0.55,
    max_token_price=0.74,
    min_token_price=0.54,
    eval_start_secs=150,
    eval_end_secs=30,
    priority=5,
    confirm_ticks=4,
    early_exit_profit=0.08,
    max_spread=0.05,
    min_score_gap=0.65,
)

ALL_ASSETS: dict[str, AssetConfig] = {
    "btc": BTC,
    "sol": SOL,
    "eth": ETH,
    "xrp": XRP,
    "doge": DOGE,
}


def get_asset(name: str) -> AssetConfig:
    key = name.lower().strip()
    if key not in ALL_ASSETS:
        raise ValueError(f"Unknown asset: {name}. Available: {', '.join(ALL_ASSETS)}")
    return ALL_ASSETS[key]



def get_enabled_assets() -> list[AssetConfig]:
    return sorted((a for a in ALL_ASSETS.values() if a.enabled), key=lambda a: a.priority)
