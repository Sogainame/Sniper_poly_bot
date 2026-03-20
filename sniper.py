"""
Core Sniper engine v2 — Brownian CDF + Edge Detection + Drawdown Stop.

Changes from v1:
  - Kelly uses true_prob from Brownian model (not arbitrary confidence mapping)
  - Edge detection: only trade when true_prob - token_price > 5%
  - Drawdown stop: pause if lost > 20% of starting balance
  - OBI (order book imbalance) filter
  - Signal engine receives secs_remaining for time-aware analysis
"""

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from assets import AssetConfig
from signal_engine import Signal, SignalEngine, MIN_EDGE, MIN_EDGE_STRONG
from market import PolymarketClient
from notifier import send_telegram

WINDOW_SECS = 300
DRY_RUN_BALANCE = 20.0
MIN_BET_USD = 1.0
BALANCE_RESERVE = 0.50
DRAWDOWN_LIMIT = 0.20      # Stop if lost 20% of starting balance

MODES = {
    "safe":       {"kelly_fraction": 0.25, "max_bet_pct": 0.25, "label": "Safe (¼ Kelly)"},
    "aggressive": {"kelly_fraction": 0.50, "max_bet_pct": 0.50, "label": "Aggressive (½ Kelly)"},
    "degen":      {"kelly_fraction": 1.00, "max_bet_pct": 1.00, "label": "Degen (full Kelly)"},
}

CSV_DIR = Path("data/sniper")


def _slug_short(slug: str) -> str:
    parts = slug.split("-updown-")
    return parts[-1] if len(parts) > 1 else slug


@dataclass
class WindowState:
    window_ts: int = 0
    slug: str = ""
    up_token: str = ""
    down_token: str = ""
    condition_id: str = ""
    open_price: float = 0.0
    open_captured: bool = False
    best_signal: Signal = field(default_factory=Signal)
    best_edge: float = 0.0
    fired: bool = False
    fire_side: str = ""
    fire_price: float = 0.0
    fire_shares: float = 0.0
    fire_edge: float = 0.0
    fire_true_prob: float = 0.0
    order_id: str | None = None


@dataclass
class Stats:
    windows: int = 0
    fired: int = 0
    skipped: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    starting_balance: float = 0.0
    drawdown_stops: int = 0


class Sniper:

    def __init__(self, asset: AssetConfig, client: PolymarketClient,
                 dry_run: bool = True, mode: str = "safe", max_bet: float = 50.0):
        self.asset = asset
        self.client = client
        self.dry_run = dry_run
        self.mode_cfg = MODES[mode]
        self.mode_name = mode
        self.max_bet = max_bet
        self.engine = SignalEngine(asset_name=asset.slug_prefix)
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        self._drawdown_paused = False
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    # ── Timing ────────────────────────────────────────────────────────────

    def _window_ts(self) -> int:
        now = time.time()
        return int(now - (now % WINDOW_SECS))

    def _secs_left(self) -> float:
        return self._window_ts() + WINDOW_SECS - time.time()

    # ── Drawdown check ────────────────────────────────────────────────────

    def _check_drawdown(self) -> bool:
        """Returns True if we should stop trading (drawdown exceeded)."""
        if self.stats.starting_balance <= 0:
            return False
        if self.stats.pnl < 0:
            loss_pct = abs(self.stats.pnl) / self.stats.starting_balance
            if loss_pct >= DRAWDOWN_LIMIT:
                if not self._drawdown_paused:
                    self._drawdown_paused = True
                    self.stats.drawdown_stops += 1
                    msg = (f"🛑 {self.asset.name}: Drawdown stop!"
                           f" Lost ${abs(self.stats.pnl):.2f}"
                           f" ({loss_pct:.0%} of starting balance)")
                    print(f"\n  [{self.asset.name}] {msg}")
                    send_telegram(msg)
                return True
        self._drawdown_paused = False
        return False

    # ── Kelly with Brownian true_prob ─────────────────────────────────────

    def _kelly_bet(self, true_prob: float, token_price: float) -> float:
        """
        Kelly criterion using Brownian-derived true probability.
        f* = (b*p - q) / b
        where b = payout odds, p = true_prob, q = 1 - true_prob
        """
        if token_price <= 0.01 or token_price >= 0.99:
            return 0.0
        if true_prob <= 0.50 or true_prob >= 0.99:
            return 0.0

        b = (1.0 / token_price) - 1.0   # net profit per $1 if win
        p = true_prob
        q = 1.0 - p
        kelly = (b * p - q) / b

        if kelly <= 0:
            return 0.0

        # Get balance
        bal = self.client.get_balance()
        if self.dry_run and (bal is None or bal < MIN_BET_USD):
            bal = DRY_RUN_BALANCE
        if bal is None or bal < MIN_BET_USD:
            return 0.0

        available = bal - BALANCE_RESERVE
        if available < MIN_BET_USD:
            return 0.0

        # Apply mode fraction
        kelly *= self.mode_cfg["kelly_fraction"]
        cap = available * self.mode_cfg["max_bet_pct"]
        bet = min(available * kelly, cap, self.max_bet)
        return bet if bet >= MIN_BET_USD else 0.0

    # ── OBI (Order Book Imbalance) ────────────────────────────────────────

    def _get_obi(self, up_token: str, down_token: str, direction: str) -> float:
        """
        Order Book Imbalance: ratio of bids on our side vs opposite.
        OBI > 0.6 = market agrees with us. OBI < 0.4 = market disagrees.
        Returns OBI for the direction we want to trade (0-1).
        """
        try:
            our_token = up_token if direction == "UP" else down_token
            our_book = self.client.fetch_book(our_token)
            opp_token = down_token if direction == "UP" else up_token
            opp_book = self.client.fetch_book(opp_token)

            our_bid = our_book.get("best_bid", 0)
            opp_bid = opp_book.get("best_bid", 0)

            if our_bid + opp_bid > 0:
                return our_bid / (our_bid + opp_bid)
        except Exception:
            pass
        return 0.5  # neutral

    # ── Fire ──────────────────────────────────────────────────────────────

    def _fire(self, sig: Signal):
        s = self.state
        if s.fired:
            return
        s.fired = True
        a = self.asset

        token = s.up_token if sig.direction == "UP" else s.down_token
        buy_price = self.client.get_buy_price(token, a.max_token_price, a.min_token_price)

        # Retry
        if buy_price <= 0:
            time.sleep(0.5)
            buy_price = self.client.get_buy_price(token, a.max_token_price, a.min_token_price)

        # DRY RUN fallback: estimate from delta
        if buy_price <= 0 and self.dry_run:
            ad = abs(sig.delta_pct)
            if ad >= 0.15:
                buy_price = 0.82
            elif ad >= 0.10:
                buy_price = 0.75
            elif ad >= 0.05:
                buy_price = 0.65
            elif ad >= 0.02:
                buy_price = 0.55
            else:
                buy_price = 0.50

        if buy_price <= 0:
            print(f"  [{a.name}] [SKIP] No price for {sig.direction}")
            self.stats.skipped += 1
            return

        # ── Edge detection ────────────────────────────────────────────
        edge = self.engine.calc_edge(sig, buy_price)

        if edge < MIN_EDGE:
            print(f"  [{a.name}] [SKIP] Edge {edge:.1%} < {MIN_EDGE:.0%}"
                  f" (prob={sig.true_prob:.1%} price={buy_price:.2f})")
            self.stats.skipped += 1
            return

        # ── OBI filter (soft) ─────────────────────────────────────────
        obi = self._get_obi(s.up_token, s.down_token, sig.direction)
        if obi < 0.30:
            print(f"  [{a.name}] [SKIP] OBI={obi:.2f} — book disagrees")
            self.stats.skipped += 1
            return

        # ── Kelly sizing with Brownian true_prob ──────────────────────
        bet = self._kelly_bet(sig.true_prob, buy_price)
        if bet < MIN_BET_USD:
            print(f"  [{a.name}] [SKIP] Kelly=0 (prob={sig.true_prob:.1%}"
                  f" price={buy_price:.2f})")
            self.stats.skipped += 1
            return

        # Scale up for strong edge
        if edge >= MIN_EDGE_STRONG:
            bet = min(bet * 1.5, self.max_bet)

        shares = max(bet / buy_price, 5)  # Polymarket min 5 shares
        cost = shares * buy_price
        roi = ((1.0 - buy_price) / buy_price) * 100
        sl = self._secs_left()

        s.fire_side = sig.direction
        s.fire_price = buy_price
        s.fire_shares = shares
        s.fire_edge = edge
        s.fire_true_prob = sig.true_prob
        s.best_signal = sig

        print(f"\n  [{a.name}] 🎯 FIRE: {sig.direction} @ {buy_price:.2f}"
              f" x {shares:.0f}sh = ${cost:.2f}")
        print(f"  [{a.name}]    P={sig.true_prob:.0%} Edge={edge:.1%}"
              f" ROI={roi:.1f}% z={sig.z_score:+.2f}"
              f" OBI={obi:.2f} T-{sl:.0f}s")

        if self.dry_run:
            s.order_id = f"DRY-{a.name}-{sig.direction}-{s.window_ts}"
            print(f"  [{a.name}] [DRY] Would FOK BUY {sig.direction} ${cost:.2f}")
        else:
            # FOK: fill instantly or cancel. amount = dollars to spend.
            # max_price = slippage protection (buy_price already capped by max_token_price)
            s.order_id = self.client.submit_fok_buy(
                token, cost, buy_price, f"{a.name}-{sig.direction}")

        self.stats.fired += 1
        if not self.dry_run:
            send_telegram(
                f"🎯 {a.name}: {sig.direction} @ {buy_price:.2f}"
                f" x {shares:.0f}sh = ${cost:.2f}\n"
                f"P={sig.true_prob:.0%} Edge={edge:.1%}"
                f" z={sig.z_score:+.2f} OBI={obi:.2f}\n"
                f"{_slug_short(s.slug)} T-{sl:.0f}s | {self.mode_name}")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _on_window_start(self):
        ts = self._window_ts()
        self.state = WindowState(window_ts=ts)
        self.engine.reset()
        a = self.asset

        price = self.client.fetch_price(a.binance_symbol)
        if price > 0:
            self.state.open_price = price
            self.state.open_captured = True
            print(f"\n  [{a.name}] [OPEN] Window {ts} | {a.name}: ${price:,.4f}")

        market = self.client.find_market(a.slug_prefix, ts)
        if market:
            self.state.slug = market["slug"]
            self.state.up_token = market["token_ids"][0]
            self.state.down_token = market["token_ids"][1]
            self.state.condition_id = market.get("condition_id", "")
            print(f"  [{a.name}] [MARKET] {_slug_short(market['slug'])}")

    def _on_window_end(self):
        self.stats.windows += 1
        if not self.state.fired:
            return
        time.sleep(3)
        self._check_result()

    def _check_result(self):
        s = self.state
        a = self.asset
        if not s.fire_side:
            return

        close_price = self.client.fetch_price(a.binance_symbol)
        gap = close_price - s.open_price if close_price > 0 else 0

        if close_price > 0:
            actual = "UP" if gap >= 0 else "DOWN"
            won = (s.fire_side == actual)
        else:
            token = s.up_token if s.fire_side == "UP" else s.down_token
            won = self.client.fetch_midpoint(token) >= 0.70

        if won:
            profit = round(s.fire_shares * (1.0 - s.fire_price) * 0.98, 2)
            self.stats.wins += 1
            self.stats.pnl += profit
            result = "WIN"
            print(f"  [{a.name}] ✅ WIN: {s.fire_side} → +${profit:.2f}"
                  f" | {a.name}: ${close_price:,.4f} ({gap:+,.4f})")
        else:
            loss = round(s.fire_shares * s.fire_price, 2)
            self.stats.losses += 1
            self.stats.pnl -= loss
            profit = -loss
            result = "LOSS"
            print(f"  [{a.name}] ❌ LOSS: {s.fire_side} → -${loss:.2f}"
                  f" | {a.name}: ${close_price:,.4f} ({gap:+,.4f})")

        if not self.dry_run:
            send_telegram(f"{'✅' if won else '❌'} {a.name} {result}:"
                          f" {s.fire_side} → ${profit:+.2f}")
        self._log(result, profit, close_price, gap)

    def _log(self, result, pnl, close_price, gap):
        a = self.asset
        csv_path = CSV_DIR / f"trades_{a.name.lower()}.csv"
        header = not csv_path.exists()
        s = self.state
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            if header:
                w.writerow(["ts", "window", "slug", "side", "mode",
                            "open", "close", "gap", "delta_pct",
                            "true_prob", "edge", "z_score",
                            "price", "shares", "cost",
                            "result", "pnl", "cum_pnl"])
            w.writerow([now, s.window_ts, _slug_short(s.slug), s.fire_side,
                        self.mode_name, f"{s.open_price:.4f}",
                        f"{close_price:.4f}", f"{gap:+.4f}",
                        f"{s.best_signal.delta_pct:+.4f}",
                        f"{s.fire_true_prob:.3f}", f"{s.fire_edge:.3f}",
                        f"{s.best_signal.z_score:+.2f}",
                        f"{s.fire_price:.2f}", f"{s.fire_shares:.0f}",
                        f"{s.fire_shares * s.fire_price:.2f}",
                        result, f"{pnl:.2f}", f"{self.stats.pnl:.2f}"])

    # ── Main loop ─────────────────────────────────────────────────────────

    def run(self):
        a = self.asset
        mode_label = "LIVE" if not self.dry_run else "DRY"
        bal = self.client.get_balance()
        if bal and bal > 0:
            self.stats.starting_balance = bal
        bal_s = f"${bal:.2f}" if bal else "n/a"

        print(f"\n{'─' * 60}")
        print(f"  🎯 {a.name} Sniper v2 — {mode_label} {self.mode_name}")
        print(f"  Model: Brownian CDF + Edge ≥ {MIN_EDGE:.0%}")
        print(f"  σ={self.engine.sigma*100:.2f}% | Kelly {self.mode_cfg['kelly_fraction']:.0%}"
              f" | Drawdown stop: {DRAWDOWN_LIMIT:.0%}")
        print(f"  Eval T-{a.eval_start_secs}→T-{a.eval_end_secs}"
              f" | Token [{a.min_token_price:.2f}, {a.max_token_price:.2f}]"
              f" | Bal: {bal_s}")
        print(f"{'─' * 60}")

        self.running = True
        last_ts = 0
        last_tick = 0.0

        while self.running:
            now = time.time()
            cur_ts = self._window_ts()
            sl = self._secs_left()
            s = self.state

            if cur_ts != last_ts:
                if last_ts > 0:
                    self._on_window_end()
                last_ts = cur_ts
                self._on_window_start()

            # Drawdown check
            if self._check_drawdown():
                time.sleep(5)
                continue

            if not s.open_captured and sl > WINDOW_SECS - 10:
                p = self.client.fetch_price(a.binance_symbol)
                if p > 0:
                    s.open_price = p
                    s.open_captured = True

            in_eval = a.eval_end_secs <= sl <= a.eval_start_secs

            if in_eval and not s.fired and s.open_captured and s.up_token:
                if now - last_tick >= 2.0:
                    p = self.client.fetch_price(a.binance_symbol)
                    last_tick = now
                    if p > 0:
                        self.engine.add_tick(p, now)
                        sig = self.engine.analyze(s.open_price, p, sl)

                        if sig.true_prob > s.best_signal.true_prob:
                            s.best_signal = sig

                        # Fetch LIVE token price to compute real edge
                        # Use get_buy_price (best_ask) — same as _fire() uses
                        edge_str = ""
                        live_edge = 0.0
                        if sig.direction and len(self.engine.tick_prices) >= 3:
                            token_id = s.up_token if sig.direction == "UP" else s.down_token
                            token_price = self.client.get_buy_price(
                                token_id, a.max_token_price, a.min_token_price)
                            if token_price > 0.01:
                                live_edge = sig.true_prob - token_price
                                edge_str = f" E={live_edge:+.0%} ask={token_price:.2f}"

                                # Track best edge seen
                                if live_edge > s.best_edge:
                                    s.best_edge = live_edge

                        d = "▲" if sig.direction == "UP" else "▼" if sig.direction == "DOWN" else "━"
                        print(f"  [{a.name}] {d} ${p:,.2f}"
                              f" P={sig.true_prob:.0%}"
                              f" z={sig.z_score:+.2f}"
                              f" Δ={sig.delta_pct:+.3f}%"
                              f"{edge_str}"
                              f" T-{sl:.0f}s", end="\r")

                        # ── Fire logic: wait for time-decay edge ──────────
                        has_ticks = len(self.engine.tick_prices) >= 3

                        # Primary: fire when we have real edge from live token price
                        if (has_ticks and live_edge >= MIN_EDGE
                                and sig.true_prob >= 0.55
                                and abs(sig.delta_pct) >= a.min_delta_pct):
                            print(f"\n  [{a.name}] [FIRE] P={sig.true_prob:.0%}"
                                  f" Edge={live_edge:.1%} z={sig.z_score:+.2f}"
                                  f" T-{sl:.0f}s")
                            self._fire(sig)

                        # Deadline: at T-end, fire if best signal was strong
                        elif (sl <= a.eval_end_secs + 2
                              and s.best_signal.true_prob >= 0.60
                              and s.best_edge >= 0.02):
                            print(f"\n  [{a.name}] [DEADLINE] P={s.best_signal.true_prob:.0%}"
                                  f" BestEdge={s.best_edge:.1%}")
                            self._fire(s.best_signal)

            elif not in_eval and not s.fired and sl > a.eval_start_secs:
                if now - last_tick >= 2.0:
                    p = self.client.fetch_price(a.binance_symbol)
                    last_tick = now
                    if p > 0 and s.open_captured:
                        g = p - s.open_price
                        d = "▲" if g > 0 else "▼" if g < 0 else "━"
                        print(f"  [{a.name}] {d} ${p:,.2f}"
                              f" Gap={g:+,.2f}"
                              f" Eval in {sl - a.eval_start_secs:.0f}s",
                              end="\r")

            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                self.running = False
                break

        self._on_window_end()

    def summary(self) -> str:
        s = self.stats
        wr = s.wins / (s.wins + s.losses) * 100 if (s.wins + s.losses) > 0 else 0
        return (f"{self.asset.name}: {s.fired} trades"
                f" W/L={s.wins}/{s.losses} ({wr:.0f}%)"
                f" PnL=${s.pnl:+.2f}")
