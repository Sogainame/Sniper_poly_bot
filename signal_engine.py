"""
Composite Signal Engine for 5-minute binary markets.

Weights based on Archetapp research + real trading results:
  - Window delta is king (weight 6-7)
  - Short-term TA (EMA, RSI) is noise at 5-min scale
  - Micro momentum and tick consistency are supporting signals
"""

from dataclasses import dataclass, field


# ── Weights ───────────────────────────────────────────────────────────────────
W_DELTA = 6.0
W_MOMENTUM = 2.0
W_ACCELERATION = 1.5
W_CONSISTENCY = 1.5

MAX_POSSIBLE = W_DELTA + W_MOMENTUM + W_ACCELERATION + W_CONSISTENCY


@dataclass
class Signal:
    direction: str = ""        # "UP" or "DOWN" or ""
    score: float = 0.0         # Raw weighted score
    confidence: float = 0.0    # 0.0 - 1.0
    delta_pct: float = 0.0     # Asset % move from open
    delta_usd: float = 0.0     # Asset $ move from open
    components: dict = field(default_factory=dict)


class SignalEngine:

    def __init__(self):
        self.tick_prices: list[float] = []
        self.tick_times: list[float] = []

    def reset(self):
        self.tick_prices.clear()
        self.tick_times.clear()

    def add_tick(self, price: float, ts: float):
        self.tick_prices.append(price)
        self.tick_times.append(ts)

    def analyze(self, open_price: float, current_price: float) -> Signal:
        if open_price <= 0 or current_price <= 0:
            return Signal()

        sig = Signal()
        delta_pct = (current_price - open_price) / open_price * 100
        sig.delta_pct = delta_pct
        sig.delta_usd = current_price - open_price

        abs_delta = abs(delta_pct)
        if abs_delta >= 0.15:
            ds = 7.0
        elif abs_delta >= 0.10:
            ds = 6.0
        elif abs_delta >= 0.05:
            ds = 4.0
        elif abs_delta >= 0.02:
            ds = 2.5
        elif abs_delta >= 0.01:
            ds = 1.0
        else:
            ds = 0.0

        sign = 1.0 if delta_pct >= 0 else -1.0
        sig.components["delta"] = round(ds * sign, 1)

        # Micro Momentum
        mom = 0.0
        if len(self.tick_prices) >= 3:
            recent = self.tick_prices[-3:]
            moves = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
            up = sum(1 for m in moves if m > 0)
            down = sum(1 for m in moves if m < 0)
            if up > down:
                mom = W_MOMENTUM
            elif down > up:
                mom = -W_MOMENTUM
        sig.components["mom"] = round(mom, 1)

        # Acceleration
        acc = 0.0
        if len(self.tick_prices) >= 4:
            prev = self.tick_prices[-3] - self.tick_prices[-4]
            curr = self.tick_prices[-1] - self.tick_prices[-2]
            if abs(curr) > abs(prev) * 1.2:
                acc = W_ACCELERATION if curr > 0 else -W_ACCELERATION
            elif abs(prev) > 0 and abs(curr) < abs(prev) * 0.5:
                acc = -W_ACCELERATION * 0.3 if curr > 0 else W_ACCELERATION * 0.3
        sig.components["acc"] = round(acc, 1)

        # Tick Consistency
        cons = 0.0
        if len(self.tick_prices) >= 5:
            deltas = [self.tick_prices[i+1] - self.tick_prices[i]
                      for i in range(len(self.tick_prices)-1)]
            if delta_pct > 0:
                agree = sum(1 for d in deltas if d > 0)
            else:
                agree = sum(1 for d in deltas if d < 0)
            ratio = agree / len(deltas) if deltas else 0
            if ratio >= 0.6:
                cons = W_CONSISTENCY * sign
            elif ratio <= 0.3:
                cons = -W_CONSISTENCY * 0.5 * sign
        sig.components["cons"] = round(cons, 1)

        total = sig.components["delta"] + mom + acc + cons
        sig.score = round(total, 2)
        sig.direction = "UP" if total > 0 else "DOWN" if total < 0 else ""
        sig.confidence = min(abs(total) / (MAX_POSSIBLE * 0.6), 1.0)

        return sig
