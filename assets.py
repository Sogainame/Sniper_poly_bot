"""
Per-asset configuration for Polymarket 5-minute Up/Down markets.

Each asset has tuned thresholds based on:
  - Observed 5-min volatility
  - Polymarket liquidity depth
  - Real trading results (176-trade study: ETH/XRP = net losers)
  - Binance symbol for price feed
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    name: str                # Display name
    slug_prefix: str         # Polymarket slug prefix: {prefix}-updown-5m-{ts}
    binance_symbol: str      # Binance ticker symbol
    chainlink_symbol: str    # Chainlink RTDS symbol (for future WS use)
    min_delta_pct: float     # Minimum BTC % move to consider trading
    min_confidence: float    # Minimum composite confidence to fire
    max_token_price: float   # Don't buy above this (ROI too low)
    min_token_price: float   # Don't buy if market is undecided
    eval_start_secs: int     # Start eval at T-X seconds
    eval_end_secs: int       # Stop eval at T-X seconds
    priority: int            # 1=best, 5=worst (for multi-asset scheduling)
    enabled: bool = True


# ── Asset Profiles ────────────────────────────────────────────────────────────

BTC = AssetConfig(
    name="BTC",
    slug_prefix="btc",
    binance_symbol="BTCUSDT",
    chainlink_symbol="btc/usd",
    min_delta_pct=0.02,
    min_confidence=0.30,
    max_token_price=0.93,
    min_token_price=0.52,
    eval_start_secs=60,
    eval_end_secs=10,
    priority=1,
)

SOL = AssetConfig(
    name="SOL",
    slug_prefix="sol",
    binance_symbol="SOLUSDT",
    chainlink_symbol="sol/usd",
    min_delta_pct=0.03,
    min_confidence=0.35,
    max_token_price=0.92,
    min_token_price=0.52,
    eval_start_secs=55,
    eval_end_secs=10,
    priority=2,
)

ETH = AssetConfig(
    name="ETH",
    slug_prefix="eth",
    binance_symbol="ETHUSDT",
    chainlink_symbol="eth/usd",
    min_delta_pct=0.04,
    min_confidence=0.45,
    max_token_price=0.91,
    min_token_price=0.53,
    eval_start_secs=50,
    eval_end_secs=10,
    priority=3,
)

XRP = AssetConfig(
    name="XRP",
    slug_prefix="xrp",
    binance_symbol="XRPUSDT",
    chainlink_symbol="xrp/usd",
    min_delta_pct=0.05,
    min_confidence=0.50,
    max_token_price=0.90,
    min_token_price=0.53,
    eval_start_secs=50,
    eval_end_secs=12,
    priority=4,
)

DOGE = AssetConfig(
    name="DOGE",
    slug_prefix="doge",
    binance_symbol="DOGEUSDT",
    chainlink_symbol="doge/usd",
    min_delta_pct=0.08,
    min_confidence=0.55,
    max_token_price=0.88,
    min_token_price=0.54,
    eval_start_secs=45,
    eval_end_secs=12,
    priority=5,
)

# ── Registry ──────────────────────────────────────────────────────────────────

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
        raise ValueError(f"Unknown asset: {name}. Available: {list(ALL_ASSETS.keys())}")
    return ALL_ASSETS[key]


def get_enabled_assets() -> list[AssetConfig]:
    return sorted(
        [a for a in ALL_ASSETS.values() if a.enabled],
        key=lambda a: a.priority,
    )
