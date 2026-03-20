"""
Core Sniper engine — runs one asset at a time.
Takes AssetConfig for per-asset thresholds.

Step 3 changes:
- removes fake Kelly sizing (there is no calibrated win probability yet)
- adds a deterministic risk ladder based on signal quality
- adds alignment checks so the bot avoids firing on internally contradictory signals
- requires confirmation across ticks, not just one noisy score spike
- tightens late-window fallback so weak end-of-window guesses do not fire
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
EARLY_EXIT_GAIN = 0.10
MIN_SCORE_TO_FIRE = 3.5
STRONG_SCORE_TO_FIRE = 5.5
MIN_CONFIRM_TICKS = 2

MODES = {
    "safe": {
        "min_risk_pct": 0.03,
        "max_risk_pct": 0.08,
        "hard_cap_pct": 0.12,
        "label": "Safe",
    },
    "aggressive": {
        "min_risk_pct": 0.05,
        "max_risk_pct": 0.14,
        "hard_cap_pct": 0.20,
        "label": "Aggressive",
    },
    "degen": {
        "min_risk_pct": 0.08,
        "max_risk_pct": 0.22,
        "hard_cap_pct": 0.30,
        "label": "Degen",
    },
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
    early_sold: bool = False
    confirm_ticks: int = 0
    last_direction: str = ""
    last_quality: float = 0.0


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

    # ── Signal gating / sizing ────────────────────────────────────────────

    def _available_balance(self) -> float:
        bal = self.client.get_balance()
        if self.dry_run and (bal is None or bal < MIN_BET_USD):
            bal = DRY_RUN_BALANCE
        if bal is None:
            return 0.0
        return max(0.0, bal - BALANCE_RESERVE)

    def _signal_alignment_ok(self, sig: Signal) -> bool:
        if sig.direction not in ("UP", "DOWN"):
            return False

        sign = 1.0 if sig.direction == "UP" else -1.0
        mom = sig.components.get("mom", 0.0) * sign
        acc = sig.components.get("acc", 0.0) * sign
        cons = sig.components.get("cons", 0.0) * sign

        # If supporting components actively disagree with the trade direction,
        # skip the trade unless the total score is very strong.
        if mom < -0.5:
            return False
        if cons < -0.25:
            return False
        if acc < -0.75 and abs(sig.score) < STRONG_SCORE_TO_FIRE:
            return False
        return True

    def _signal_quality(self, sig: Signal) -> float:
        a = self.asset
        if sig.direction not in ("UP", "DOWN"):
            return 0.0

        score_q = min(max((abs(sig.score) - MIN_SCORE_TO_FIRE) / 4.0, 0.0), 1.0)
        conf_floor = max(a.min_confidence, 0.20)
        conf_q = min(max((sig.confidence - conf_floor) / max(1e-6, 1.0 - conf_floor), 0.0), 1.0)
        delta_floor = max(a.min_delta_pct, 1e-6)
        delta_q = min(max((abs(sig.delta_pct) - delta_floor) / (3.0 * delta_floor), 0.0), 1.0)

        quality = 0.45 * score_q + 0.35 * conf_q + 0.20 * delta_q
        if self._signal_alignment_ok(sig):
            quality += 0.10
        else:
            quality -= 0.30
        return min(max(quality, 0.0), 1.0)

    def _stake_size(self, sig: Signal, token_price: float) -> float:
        a = self.asset
        if token_price <= 0.01 or token_price >= 0.99:
            return 0.0

        available = self._available_balance()
        if available < MIN_BET_USD:
            return 0.0

        quality = self._signal_quality(sig)
        if quality < 0.20:
            return 0.0

        risk_pct = self.mode_cfg["min_risk_pct"] + quality * (
            self.mode_cfg["max_risk_pct"] - self.mode_cfg["min_risk_pct"]
        )

        # Penalize expensive contracts unless the signal is genuinely strong.
        if token_price >= min(0.82, a.max_token_price) and quality < 0.55:
            risk_pct *= 0.5

        # Penalize signals that only barely exceed the minimum delta filter.
        if abs(sig.delta_pct) < a.min_delta_pct * 1.25:
            risk_pct *= 0.6

        bet = min(
            self.max_bet,
            available * self.mode_cfg["hard_cap_pct"],
            available * risk_pct,
        )
        return bet if bet >= MIN_BET_USD else 0.0

    def _update_confirmation(self, sig: Signal):
        s = self.state
        a = self.asset
        basic_ok = (
            sig.direction in ("UP", "DOWN")
            and abs(sig.delta_pct) >= a.min_delta_pct
            and sig.confidence >= a.min_confidence
            and abs(sig.score) >= MIN_SCORE_TO_FIRE
            and self._signal_alignment_ok(sig)
        )

        if not basic_ok:
            s.confirm_ticks = 0
            s.last_direction = ""
            s.last_quality = 0.0
            return

        quality = self._signal_quality(sig)
        if sig.direction == s.last_direction and quality >= s.last_quality * 0.80:
            s.confirm_ticks += 1
        else:
            s.confirm_ticks = 1
        s.last_direction = sig.direction
        s.last_quality = quality

    def _should_fire(self, sig: Signal, secs_left: float) -> bool:
        a = self.asset
        if sig.direction not in ("UP", "DOWN"):
            return False
        if abs(sig.delta_pct) < a.min_delta_pct:
            return False
        if sig.confidence < a.min_confidence:
            return False
        if abs(sig.score) < MIN_SCORE_TO_FIRE:
            return False
        if not self._signal_alignment_ok(sig):
            return False

        quality = self._signal_quality(sig)
        strong = quality >= 0.65 and abs(sig.score) >= STRONG_SCORE_TO_FIRE
        confirmed = self.state.confirm_ticks >= MIN_CONFIRM_TICKS and quality >= 0.30
        late_strong = (
            secs_left <= a.eval_end_secs + 6
            and self.state.confirm_ticks >= 1
            and quality >= 0.50
            and abs(sig.score) >= 4.5
        )
        return strong or confirmed or late_strong

    # ── Fire ──────────────────────────────────────────────────────────────

    def _fire(self, sig: Signal):
        s = self.state
        if s.fired:
            return
        a = self.asset

        token = s.up_token if sig.direction == "UP" else s.down_token
        buy_price = self.client.get_buy_price(token, a.max_token_price, a.min_token_price)

        # Retry once after 0.5s if price not available
        if buy_price <= 0:
            time.sleep(0.5)
            buy_price = self.client.get_buy_price(token, a.max_token_price, a.min_token_price)

        if buy_price <= 0:
            print(f"  [{a.name}] [SKIP] No executable ask for {sig.direction}")
            self.stats.skipped += 1
            return

        bet = self._stake_size(sig, buy_price)
        if bet < MIN_BET_USD:
            print(f"  [{a.name}] [SKIP] No-trade zone"
                  f" (score={sig.score:+.1f} conf={sig.confidence:.0%}"
                  f" price={buy_price:.2f} q={self._signal_quality(sig):.2f})")
            self.stats.skipped += 1
            return

        # All checks passed — NOW lock the window
        s.fired = True

        shares = max(bet / buy_price, 5)
        cost = shares * buy_price
        roi = ((1.0 - buy_price) / buy_price) * 100
        sl = self._secs_left()
        quality = self._signal_quality(sig)

        s.fire_side = sig.direction
        s.fire_price = buy_price
        s.fire_shares = shares
        s.fire_confidence = sig.confidence
        s.best_signal = sig

        print(f"\n  [{a.name}] 🎯 FIRE: {sig.direction} @ {buy_price:.2f}"
              f" x {shares:.0f}sh = ${cost:.2f}")
        print(f"  [{a.name}]    Δ={sig.delta_pct:+.3f}%"
              f" Conf={sig.confidence:.0%} Score={sig.score:+.1f}"
              f" Q={quality:.2f} ROI={roi:.1f}% T-{sl:.0f}s")

        if self.dry_run:
            s.order_id = f"DRY-{a.name}-{sig.direction}-{s.window_ts}"
            print(f"  [{a.name}] [DRY] Would TAKER BUY {sig.direction}")
            self.stats.fired += 1
        else:
            s.order_id = self.client.submit_maker_buy(
                token, buy_price, shares, f"{a.name}-{sig.direction}")
            if s.order_id:
                self.stats.fired += 1
                bal = self.client.get_balance()
                if bal is not None:
                    print(f"  [{a.name}]    💰 Balance: ${bal:.2f}")
            else:
                print(f"  [{a.name}] [!] Order failed — unfiring window")
                s.fired = False
                return
        if not self.dry_run:
            send_telegram(
                f"🎯 {a.name}: {sig.direction} @ {buy_price:.2f}"
                f" x {shares:.0f}sh = ${cost:.2f}\n"
                f"Δ={sig.delta_pct:+.3f}% Conf={sig.confidence:.0%}"
                f" Score={sig.score:+.1f} Q={quality:.2f}"
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
            t_start = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")
            t_end = datetime.fromtimestamp(ts + WINDOW_SECS, timezone.utc).strftime("%H:%M UTC")
            print(f"\n  [{a.name}] ── {t_start}-{t_end} ── {a.name}: ${price:,.2f}")

        market = self.client.find_market(a.slug_prefix, ts)
        if market:
            self.state.slug = market["slug"]
            self.state.up_token = market["up_token"]
            self.state.down_token = market["down_token"]
            self.state.condition_id = market.get("condition_id", "")
        else:
            print(f"  [{a.name}] [!] Market not found for ts={ts}")

    def _on_window_end(self):
        self.stats.windows += 1
        if not self.state.fired or self.state.early_sold:
            return
        time.sleep(3)
        self._check_result()

    def _check_result(self):
        s = self.state
        a = self.asset
        if not s.fire_side:
            return

        up_mid = self.client.fetch_midpoint(s.up_token)
        down_mid = self.client.fetch_midpoint(s.down_token)

        if up_mid > 0.01 or down_mid > 0.01:
            if up_mid > down_mid:
                actual = "UP"
            elif down_mid > up_mid:
                actual = "DOWN"
            else:
                actual = ""
            won = (s.fire_side == actual) if actual else False
        else:
            close_price = self.client.fetch_price(a.binance_symbol)
            gap = close_price - s.open_price if close_price > 0 else 0
            actual = "UP" if gap >= 0 else "DOWN"
            won = (s.fire_side == actual)

        close_price = self.client.fetch_price(a.binance_symbol)
        gap = close_price - s.open_price if close_price > 0 else 0

        if won:
            profit = round(s.fire_shares * (1.0 - s.fire_price) * 0.98, 2)
            self.stats.wins += 1
            self.stats.pnl += profit
            result = "WIN"
            print(f"  [{a.name}] ✅ WIN: {s.fire_side}"
                  f" | bought {s.fire_shares:.0f}sh @ ${s.fire_price:.2f}"
                  f" → payout ${s.fire_shares:.0f} × $1.00 = +${profit:.2f}")
            print(f"  [{a.name}]    UP=${up_mid:.2f} DOWN=${down_mid:.2f}"
                  f" → resolved: {actual}")

            if not self.dry_run:
                token = s.up_token if s.fire_side == "UP" else s.down_token
                time.sleep(3)

                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    self.client.clob.update_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token))
                except Exception:
                    pass

                sell_price = self.client.get_sell_price(token)
                if sell_price < 0.90:
                    sell_price = 0.99
                sell_id = self.client.submit_sell(
                    token, sell_price, s.fire_shares,
                    f"{a.name}-{s.fire_side}-CLAIM")
                if sell_id:
                    print(f"  [{a.name}]    💰 Sold tokens → USDC recycled")
                else:
                    print(f"  [{a.name}]    ⚠ Sell failed — check positions manually")
        else:
            loss = round(s.fire_shares * s.fire_price, 2)
            self.stats.losses += 1
            self.stats.pnl -= loss
            profit = -loss
            result = "LOSS"
            print(f"  [{a.name}] ❌ LOSS: {s.fire_side}"
                  f" | bought {s.fire_shares:.0f}sh @ ${s.fire_price:.2f}"
                  f" → resolved {actual} = -${loss:.2f}")
            print(f"  [{a.name}]    UP=${up_mid:.2f} DOWN=${down_mid:.2f}")

        if not self.dry_run:
            bal = self.client.get_balance()
            bal_str = f"${bal:.2f}" if bal else "?"
            print(f"  [{a.name}]    📊 W/L={self.stats.wins}/{self.stats.losses}"
                  f" PnL=${self.stats.pnl:+.2f} Cash={bal_str}")
            send_telegram(
                f"{'✅' if won else '❌'} {a.name} {result}:"
                f" {s.fire_side} ${profit:+.2f}"
                f"\nW/L={self.stats.wins}/{self.stats.losses}"
                f" PnL=${self.stats.pnl:+.2f}")
        else:
            print(f"  [{a.name}]    📊 W/L={self.stats.wins}/{self.stats.losses}"
                  f" PnL=${self.stats.pnl:+.2f}")
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
                            "conf", "score", "quality", "confirm_ticks",
                            "price", "shares", "cost", "result", "pnl", "cum_pnl"])
            w.writerow([now, s.window_ts, _slug_short(s.slug), s.fire_side,
                        self.mode_name, f"{s.open_price:.4f}",
                        f"{close_price:.4f}", f"{gap:+.4f}",
                        f"{s.best_signal.delta_pct:+.4f}",
                        f"{s.fire_confidence:.2f}", f"{s.best_signal.score:+.1f}",
                        f"{self._signal_quality(s.best_signal):.2f}", s.confirm_ticks,
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
        print(f"  Fire: score ≥ {MIN_SCORE_TO_FIRE:.1f} | strong ≥ {STRONG_SCORE_TO_FIRE:.1f}"
              f" | confirm ≥ {MIN_CONFIRM_TICKS} ticks")
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

            if s.fired and s.fire_side and not s.early_sold and sl > 5:
                if now - last_tick >= 2.0:
                    last_tick = now
                    token = s.up_token if s.fire_side == "UP" else s.down_token
                    sell_price = self.client.get_sell_price(token)
                    if sell_price > 0.01:
                        gain = sell_price - s.fire_price
                        print(f"  [{a.name}] 📊 {s.fire_side} bid"
                              f" ${sell_price:.2f} (gain={gain:+.2f})"
                              f" T-{sl:.0f}s", end="\r")
                        if gain >= EARLY_EXIT_GAIN:
                            profit = round(s.fire_shares * gain * 0.98, 2)
                            print(f"\n  [{a.name}] 💰 EARLY EXIT: sell"
                                  f" @ {sell_price:.2f} (bought {s.fire_price:.2f})"
                                  f" +${profit:.2f}")
                            if not self.dry_run:
                                self.client.submit_sell(
                                    token, sell_price, s.fire_shares,
                                    f"{a.name}-{s.fire_side}-EARLY")
                            s.early_sold = True
                            self.stats.wins += 1
                            self.stats.pnl += profit
                            if not self.dry_run:
                                send_telegram(
                                    f"💰 {a.name} EARLY EXIT:"
                                    f" {s.fire_side} +${profit:.2f}")

            if in_eval and not s.fired and s.open_captured and s.up_token:
                if now - last_tick >= 2.0:
                    p = self.client.fetch_price(a.binance_symbol)
                    last_tick = now
                    if p > 0:
                        self.engine.add_tick(p, now)
                        sig = self.engine.analyze(s.open_price, p)
                        self._update_confirmation(sig)

                        # Keep the strongest signal that also passes the basic gating.
                        if self._signal_alignment_ok(sig):
                            if abs(sig.score) > abs(s.best_signal.score):
                                s.best_signal = sig

                        d = "▲" if sig.direction == "UP" else "▼" if sig.direction == "DOWN" else "━"
                        print(f"  [{a.name}] {d} ${p:,.4f}"
                              f" Δ={sig.delta_pct:+.3f}%"
                              f" S={sig.score:+.1f}"
                              f" C={sig.confidence:.0%}"
                              f" Q={self._signal_quality(sig):.2f}"
                              f" K={s.confirm_ticks}"
                              f" T-{sl:.0f}s", end="\r")

                        s.prev_score = sig.score
                        if self._should_fire(sig, sl):
                            print(f"\n  [{a.name}] [TRIGGER] S={sig.score:+.1f}"
                                  f" C={sig.confidence:.0%} Δ={sig.delta_pct:+.3f}%"
                                  f" Q={self._signal_quality(sig):.2f} K={s.confirm_ticks}")
                            self._fire(sig)
                        elif (
                            sl <= a.eval_end_secs + 6
                            and s.confirm_ticks >= MIN_CONFIRM_TICKS
                            and self._signal_quality(s.best_signal) >= 0.55
                            and abs(s.best_signal.score) >= 4.5
                        ):
                            print(f"\n  [{a.name}] [TRIGGER-BEST]"
                                  f" S={s.best_signal.score:+.1f}"
                                  f" C={s.best_signal.confidence:.0%}"
                                  f" Δ={s.best_signal.delta_pct:+.3f}%"
                                  f" Q={self._signal_quality(s.best_signal):.2f}")
                            self._fire(s.best_signal)

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
