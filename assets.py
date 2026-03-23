"""Per-asset configuration for short-horizon Polymarket markets.

V3: V2 structure (confirm_ticks, max_spread, early_exit_profit) merged with
V1 BTC entry parameters (eval 60→20, delta 0.02, min_price 0.52).
"""
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
    confirm_ticks: int = 3          # from V2: consecutive ticks in same direction
    early_exit_profit: float = 0.10  # from V1: gain threshold for early sell
    max_spread: float = 0.06        # from V2: skip if book spread wider
    min_score_gap: float = 0.40     # from V2: minimum score jump between ticks
    enabled: bool = True


# ── V1 proven params for BTC ─────────────────────────────────────────────────
# eval 60→20: 4 min of price action filters noise, 20s buffer before close
# min_delta 0.02: proven profitable at ≥0.08% with DOWN signals
# min_token_price 0.52: don't buy undecided markets
# max_token_price 0.88: ROI 12% net, breakeven WR=88%, our WR~90%

BTC = AssetConfig(
    name="BTC",
    slug_prefix="btc",
    binance_symbol="BTCUSDT",
    chainlink_symbol="btc/usd",
    min_delta_pct=0.05,         # raised from 0.02: all losses were delta<0.04%
    min_confidence=0.30,
    max_token_price=0.95,       # V4: maker at $0.90-0.95 (was 0.88 taker)
    min_token_price=0.70,       # V4: at T-10s market is decided, skip 50/50 (was 0.52)
    eval_start_secs=15,         # V4: entry at T-15s (was 60) — 85% direction locked
    eval_end_secs=3,            # V4: 3s buffer for order submission (was 20)
    priority=1,
    confirm_ticks=2,            # V2 addition: require 2 ticks confirming direction
    early_exit_profit=0.10,     # V1 value (V2 was 0.06)
    max_spread=0.06,            # V2 addition
    min_score_gap=0.20,         # V2 addition
)

SOL = AssetConfig(
    name="SOL",
    slug_prefix="sol",
    binance_symbol="SOLUSDT",
    chainlink_symbol="sol/usd",
    min_delta_pct=0.03,
    min_confidence=0.35,
    max_token_price=0.80,
    min_token_price=0.52,
    eval_start_secs=170,
    eval_end_secs=30,
    priority=2,
    confirm_ticks=3,
    early_exit_profit=0.10,
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
    early_exit_profit=0.10,
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
    max_token_price=0.78,
    min_token_price=0.53,
    eval_start_secs=160,
    eval_end_secs=30,
    priority=4,
    confirm_ticks=4,
    early_exit_profit=0.10,
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
    max_token_price=0.76,
    min_token_price=0.54,
    eval_start_secs=150,
    eval_end_secs=30,
    priority=5,
    confirm_ticks=4,
    early_exit_profit=0.10,
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
