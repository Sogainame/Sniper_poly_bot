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
    early_sold: bool = False


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

        bal = self.client.get_balance()
        if self.dry_run and (bal is None or bal < MIN_BET_USD):
            bal = DRY_RUN_BALANCE
        if bal is None or bal < MIN_BET_USD:
            return 0.0
        available = bal - BALANCE_RESERVE

        if kelly <= 0:
            # Kelly says no mathematical edge at this token price.
            # For high-confidence signals (>60%) with cheap tokens (<0.85),
            # use minimum bet as override — the signal may still be profitable.
            if confidence >= 0.60 and token_price <= 0.85:
                return MIN_BET_USD
            return 0.0

        kelly *= self.mode_cfg["kelly_fraction"]
        cap = available * self.mode_cfg["max_bet_pct"]
        bet = min(available * kelly, cap, self.max_bet)
        return bet if bet >= MIN_BET_USD else 0.0

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
            return  # NOT setting s.fired — can retry this window

        bet = self._kelly_bet(sig.confidence, buy_price)
        if bet < MIN_BET_USD:
            print(f"  [{a.name}] [SKIP] Kelly: no edge"
                  f" (conf={sig.confidence:.0%} price={buy_price:.2f})")
            self.stats.skipped += 1
            return  # NOT setting s.fired — can retry this window

        # All checks passed — NOW lock the window
        s.fired = True

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
            print(f"  [{a.name}] [DRY] Would TAKER BUY {sig.direction}")
            self.stats.fired += 1
        else:
            s.order_id = self.client.submit_maker_buy(
                token, buy_price, shares, f"{a.name}-{sig.direction}")
            if s.order_id:
                self.stats.fired += 1
                # Show real balance after order
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
            # Человеческое время вместо unix timestamp
            from datetime import datetime, timezone
            t_start = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")
            t_end = datetime.fromtimestamp(ts + WINDOW_SECS, timezone.utc).strftime("%H:%M UTC")
            print(f"\n  [{a.name}] ── {t_start}-{t_end} ── BTC: ${price:,.2f}")

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

        # Determine result from token prices first.
        # If token prices are unavailable, fall back to the reference spot proxy.
        up_mid = self.client.fetch_midpoint(s.up_token)
        down_mid = self.client.fetch_midpoint(s.down_token)

        if up_mid > 0.01 or down_mid > 0.01:
            # Use token prices to infer outcome
            if up_mid > down_mid:
                actual = "UP"
            elif down_mid > up_mid:
                actual = "DOWN"
            else:
                actual = ""
            won = (s.fire_side == actual) if actual else False
        else:
            # Fallback: reference spot proxy (less reliable)
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

            # Auto-sell winning tokens at best_bid to recycle back to USDC
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
                    sell_price = 0.99  # fallback после resolution
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

        # Show real balance and running stats
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

            # ── Early exit: sell position if token price rose +$0.10 ──
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
                        if gain >= 0.10:
                            actual_gain = sell_price - s.fire_price
                            profit = round(s.fire_shares * actual_gain * 0.98, 2)
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
