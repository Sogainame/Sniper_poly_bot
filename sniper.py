"""
Core Sniper engine — runs one asset at a time.
Takes AssetConfig for per-asset thresholds.
"""

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from assets import AssetConfig
from signal_engine import Signal, SignalEngine
from market import PolymarketClient
from notifier import send_telegram

WINDOW_SECS = 300
DRY_RUN_BALANCE = 20.0
MIN_BET_USD = 1.0
BALANCE_RESERVE = 0.50

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
    prev_score: float = 0.0
    fired: bool = False
    fire_side: str = ""
    fire_price: float = 0.0
    fire_shares: float = 0.0
    fire_confidence: float = 0.0
    order_id: str | None = None


@dataclass
class Stats:
    windows: int = 0
    fired: int = 0
    skipped: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0


class Sniper:

    def __init__(self, asset: AssetConfig, client: PolymarketClient,
                 dry_run: bool = True, mode: str = "safe", max_bet: float = 50.0):
        self.asset = asset
        self.client = client
        self.dry_run = dry_run
        self.mode_cfg = MODES[mode]
        self.mode_name = mode
        self.max_bet = max_bet
        self.engine = SignalEngine()
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    # ── Timing ────────────────────────────────────────────────────────────

    def _window_ts(self) -> int:
        now = time.time()
        return int(now - (now % WINDOW_SECS))

    def _secs_left(self) -> float:
        return self._window_ts() + WINDOW_SECS - time.time()

    # ── Kelly ─────────────────────────────────────────────────────────────

    def _kelly_bet(self, confidence: float, token_price: float) -> float:
        if token_price <= 0.01 or token_price >= 0.99:
            return 0.0
        win_prob = min(0.50 + confidence * 0.30, 0.90)
        b = (1.0 / token_price) - 1.0
        kelly = (b * win_prob - (1.0 - win_prob)) / b
        if kelly <= 0:
            return 0.0
        kelly *= self.mode_cfg["kelly_fraction"]

        bal = self.client.get_balance()
        if self.dry_run and (bal is None or bal < MIN_BET_USD):
            bal = DRY_RUN_BALANCE
        if bal is None or bal < MIN_BET_USD:
            return 0.0
        available = bal - BALANCE_RESERVE
        cap = available * self.mode_cfg["max_bet_pct"]
        bet = min(available * kelly, cap, self.max_bet)
        return bet if bet >= MIN_BET_USD else 0.0

    # ── Fire ──────────────────────────────────────────────────────────────

    def _fire(self, sig: Signal):
        s = self.state
        if s.fired:
            return
        s.fired = True
        a = self.asset

        token = s.up_token if sig.direction == "UP" else s.down_token
        buy_price = self.client.get_buy_price(token, a.max_token_price, a.min_token_price)
        if buy_price <= 0:
            print(f"  [{a.name}] [SKIP] No valid price for {sig.direction}")
            self.stats.skipped += 1
            return

        bet = self._kelly_bet(sig.confidence, buy_price)
        if bet < MIN_BET_USD:
            print(f"  [{a.name}] [SKIP] Kelly: no edge")
            self.stats.skipped += 1
            return

        shares = max(bet / buy_price, 5)
        cost = shares * buy_price
        roi = ((1.0 - buy_price) / buy_price) * 100
        sl = self._secs_left()

        s.fire_side = sig.direction
        s.fire_price = buy_price
        s.fire_shares = shares
        s.fire_confidence = sig.confidence
        s.best_signal = sig

        print(f"\n  [{a.name}] 🎯 FIRE: {sig.direction} @ {buy_price:.2f}"
              f" x {shares:.0f}sh = ${cost:.2f}")
        print(f"  [{a.name}]    Δ={sig.delta_pct:+.3f}%"
              f" Conf={sig.confidence:.0%} ROI={roi:.1f}% T-{sl:.0f}s")

        if self.dry_run:
            s.order_id = f"DRY-{a.name}-{sig.direction}-{s.window_ts}"
            print(f"  [{a.name}] [DRY] Would MAKER BUY {sig.direction}")
        else:
            s.order_id = self.client.submit_maker_buy(
                token, buy_price, shares, f"{a.name}-{sig.direction}")

        self.stats.fired += 1
        if not self.dry_run:
            send_telegram(
                f"🎯 {a.name}: {sig.direction} @ {buy_price:.2f}"
                f" x {shares:.0f}sh = ${cost:.2f}\n"
                f"Δ={sig.delta_pct:+.3f}% Conf={sig.confidence:.0%}"
                f" ROI={roi:.1f}% T-{sl:.0f}s\n"
                f"{_slug_short(s.slug)} | {self.mode_name}")

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
        else:
            print(f"  [{a.name}] [!] Market not found for ts={ts}")

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
                            "conf", "score", "price", "shares",
                            "cost", "result", "pnl", "cum_pnl"])
            w.writerow([now, s.window_ts, _slug_short(s.slug), s.fire_side,
                        self.mode_name, f"{s.open_price:.4f}",
                        f"{close_price:.4f}", f"{gap:+.4f}",
                        f"{s.best_signal.delta_pct:+.4f}",
                        f"{s.fire_confidence:.2f}", f"{s.best_signal.score:+.1f}",
                        f"{s.fire_price:.2f}", f"{s.fire_shares:.0f}",
                        f"{s.fire_shares * s.fire_price:.2f}",
                        result, f"{pnl:.2f}", f"{self.stats.pnl:.2f}"])

    # ── Main loop ─────────────────────────────────────────────────────────

    def run(self):
        a = self.asset
        mode_label = "LIVE" if not self.dry_run else "DRY"
        bal = self.client.get_balance()
        bal_s = f"${bal:.2f}" if bal else "n/a"

        print(f"\n{'─' * 60}")
        print(f"  🎯 {a.name} Sniper — {mode_label} {self.mode_name}")
        print(f"  Δ ≥ {a.min_delta_pct:.3f}% | Conf ≥ {a.min_confidence:.0%}"
              f" | Token [{a.min_token_price:.2f}, {a.max_token_price:.2f}]")
        print(f"  Eval T-{a.eval_start_secs}→T-{a.eval_end_secs}"
              f" | Binance: {a.binance_symbol} | Bal: {bal_s}")
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
                        sig = self.engine.analyze(s.open_price, p)

                        if abs(sig.score) > abs(s.best_signal.score):
                            s.best_signal = sig

                        d = "▲" if sig.direction == "UP" else "▼" if sig.direction == "DOWN" else "━"
                        print(f"  [{a.name}] {d} ${p:,.4f}"
                              f" Δ={sig.delta_pct:+.3f}%"
                              f" S={sig.score:+.1f}"
                              f" C={sig.confidence:.0%}"
                              f" T-{sl:.0f}s", end="\r")

                        ad = abs(sig.delta_pct)
                        fire = False

                        if (ad >= a.min_delta_pct
                                and sig.confidence >= a.min_confidence
                                and abs(sig.score) >= 3.0):
                            fire = True

                        if (abs(sig.score) >= 3.0
                                and abs(sig.score - s.prev_score) >= 1.5
                                and ad >= a.min_delta_pct):
                            fire = True

                        if (sl <= a.eval_end_secs + 2
                                and abs(s.best_signal.score) >= 2.5
                                and abs(s.best_signal.delta_pct) >= a.min_delta_pct):
                            sig = s.best_signal
                            fire = True

                        s.prev_score = sig.score
                        if fire:
                            print(f"\n  [{a.name}] [TRIGGER] S={sig.score:+.1f}"
                                  f" C={sig.confidence:.0%} Δ={sig.delta_pct:+.3f}%")
                            self._fire(sig)

            elif not in_eval and not s.fired and sl > a.eval_start_secs:
                if now - last_tick >= 2.0:
                    p = self.client.fetch_price(a.binance_symbol)
                    last_tick = now
                    if p > 0 and s.open_captured:
                        g = p - s.open_price
                        d = "▲" if g > 0 else "▼" if g < 0 else "━"
                        print(f"  [{a.name}] {d} ${p:,.4f}"
                              f" Gap={g:+,.4f}"
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
