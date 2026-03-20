"""
Signal Engine v2 — Brownian Motion CDF + Edge Detection

Mathematical model based on real Polymarket bot research:

1. Brownian Motion CDF:
   BTC modeled as geometric Brownian motion.
   true_prob = Φ(z), where z = drift / (σ * √time_remaining)
   Φ = cumulative normal distribution (scipy.stats.norm.cdf)

2. Edge Detection:
   edge = true_prob - token_price
   Only trade when edge > MIN_EDGE (default 5%)

3. Supporting signals (momentum, consistency) adjust true_prob ±3%

4. Time-aware: same delta at T-30s is much more significant than at T-170s
   because less time remains for reversal.

Sources:
  - gengar_polymarket_bot: Brownian + Kelly + CDF
  - Archetapp: window delta is king, composite weighted signal
  - 176-trade study: momentum generates biggest wins AND biggest losses
  - Quant playbook: EV gap scanning, fractional Kelly, 20% drawdown stop
"""

import math
from dataclasses import dataclass, field

# scipy is optional — fallback to math.erf approximation if not installed
try:
    from scipy.stats import norm as scipy_norm
    def normal_cdf(x: float) -> float:
        return float(scipy_norm.cdf(x))
except ImportError:
    def normal_cdf(x: float) -> float:
        """Approximation of Φ(x) using math.erf."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ── Historical 5-min BTC volatility ──────────────────────────────────────────
# Observed from Binance 1-min candles: BTC moves ~0.05-0.15% per 5 minutes.
# σ (sigma) = annualized vol / √(periods_per_year)
# BTC annualized vol ≈ 50-70%. Per 5 min: 0.65 / √(105120) ≈ 0.002 = 0.2%
# But for our model we use the 5-min standard deviation directly.
BTC_5MIN_SIGMA = 0.0020     # ~0.20% per 5 min (conservative)
ETH_5MIN_SIGMA = 0.0025     # ETH more volatile
SOL_5MIN_SIGMA = 0.0035     # SOL even more
XRP_5MIN_SIGMA = 0.0030
DOGE_5MIN_SIGMA = 0.0050    # Meme coin, very volatile

SIGMA_MAP = {
    "btc": BTC_5MIN_SIGMA,
    "sol": SOL_5MIN_SIGMA,
    "eth": ETH_5MIN_SIGMA,
    "xrp": XRP_5MIN_SIGMA,
    "doge": DOGE_5MIN_SIGMA,
}

# Minimum edge to trade (true_prob - token_price)
MIN_EDGE = 0.05             # 5% minimum edge
MIN_EDGE_STRONG = 0.10      # 10% = strong signal, increase bet

# Momentum adjustment: supporting signals can shift true_prob by ±3%
MAX_MOMENTUM_ADJUST = 0.03

WINDOW_SECS = 300


@dataclass
class Signal:
    direction: str = ""         # "UP" or "DOWN" or ""
    true_prob: float = 0.5      # Brownian CDF probability (0-1)
    edge: float = 0.0           # true_prob - token_price (>0 = profitable)
    delta_pct: float = 0.0      # Asset % move from open
    delta_usd: float = 0.0      # Asset $ move from open
    z_score: float = 0.0        # Brownian z-score
    secs_left: float = 0.0      # Seconds remaining in window
    momentum_adj: float = 0.0   # Momentum adjustment to true_prob
    components: dict = field(default_factory=dict)


class SignalEngine:

    def __init__(self, asset_name: str = "btc"):
        self.sigma = SIGMA_MAP.get(asset_name.lower(), BTC_5MIN_SIGMA)
        self.asset_name = asset_name.lower()
        self.tick_prices: list[float] = []
        self.tick_times: list[float] = []

    def reset(self):
        self.tick_prices.clear()
        self.tick_times.clear()

    def add_tick(self, price: float, ts: float):
        self.tick_prices.append(price)
        self.tick_times.append(ts)

    def analyze(self, open_price: float, current_price: float,
                secs_remaining: float) -> Signal:
        """
        Core analysis using Brownian Motion CDF.

        Args:
            open_price: BTC price at window open
            current_price: BTC price now
            secs_remaining: seconds until window closes

        Returns:
            Signal with true_prob, edge, direction
        """
        if open_price <= 0 or current_price <= 0 or secs_remaining <= 0:
            return Signal()

        sig = Signal()
        sig.secs_left = secs_remaining

        # ── 1. Brownian Motion CDF ────────────────────────────────────────
        # drift = fractional price change from open
        drift = (current_price - open_price) / open_price
        sig.delta_pct = drift * 100
        sig.delta_usd = current_price - open_price

        # Time remaining as fraction of window (1.0 = full window, 0.0 = expired)
        time_frac = max(secs_remaining / WINDOW_SECS, 0.001)

        # z-score: how many standard deviations is the current drift,
        # normalized by remaining volatility.
        # Higher z = more likely to stay on this side.
        # σ * √(time_remaining_frac) = expected remaining volatility
        remaining_vol = self.sigma * math.sqrt(time_frac)

        if remaining_vol > 0:
            z = drift / remaining_vol
        else:
            z = 100.0 if drift > 0 else -100.0  # nearly certain

        sig.z_score = z

        # Probability that price will be UP at end (P(end >= open))
        # = Φ(z) where Φ is cumulative normal distribution
        true_prob_up = normal_cdf(z)
        sig.components["cdf_raw"] = round(true_prob_up, 3)

        # ── 2. Momentum Adjustment (±3% max) ─────────────────────────────
        mom_adj = 0.0

        # Micro momentum: direction of last 3 ticks
        if len(self.tick_prices) >= 3:
            recent = self.tick_prices[-3:]
            moves = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
            up = sum(1 for m in moves if m > 0)
            down = sum(1 for m in moves if m < 0)
            if up > down:
                mom_adj += 0.015  # +1.5% toward UP
            elif down > up:
                mom_adj -= 0.015  # +1.5% toward DOWN

        # Tick consistency: what % of all ticks agree with drift direction
        if len(self.tick_prices) >= 5:
            deltas = [self.tick_prices[i+1] - self.tick_prices[i]
                      for i in range(len(self.tick_prices)-1)]
            if drift > 0:
                agree = sum(1 for d in deltas if d > 0)
            else:
                agree = sum(1 for d in deltas if d < 0)
            ratio = agree / len(deltas) if deltas else 0.5
            # Consistency > 60% → boost, < 30% → penalize
            if ratio >= 0.6:
                mom_adj += 0.015 * (1 if drift > 0 else -1)
            elif ratio <= 0.3:
                mom_adj -= 0.01 * (1 if drift > 0 else -1)

        # Clamp momentum adjustment
        mom_adj = max(-MAX_MOMENTUM_ADJUST, min(MAX_MOMENTUM_ADJUST, mom_adj))
        sig.momentum_adj = mom_adj
        sig.components["mom_adj"] = round(mom_adj, 3)

        # ── 3. Final true probability ─────────────────────────────────────
        true_prob_up_adj = max(0.01, min(0.99, true_prob_up + mom_adj))
        sig.components["true_prob_up"] = round(true_prob_up_adj, 3)

        # Direction: UP if true_prob > 50%, DOWN otherwise
        if true_prob_up_adj > 0.5:
            sig.direction = "UP"
            sig.true_prob = true_prob_up_adj
        elif true_prob_up_adj < 0.5:
            sig.direction = "DOWN"
            sig.true_prob = 1.0 - true_prob_up_adj  # P(DOWN)
        else:
            sig.direction = ""
            sig.true_prob = 0.5

        sig.components["z"] = round(z, 2)
        sig.components["rem_vol"] = round(remaining_vol * 100, 3)

        return sig

    def calc_edge(self, sig: Signal, token_price: float) -> float:
        """
        Calculate edge = true_prob - implied_prob.
        token_price IS the implied probability on Polymarket.
        Positive edge = we think outcome is more likely than market says.
        """
        if sig.direction == "":
            return 0.0
        return sig.true_prob - token_price
