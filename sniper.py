"""Core sniper engine.

V3: V2 architecture (step(), typed Book, confirm_ticks, spread check)
    + V1 execution (Kelly sizing, GTC maker orders, auto-sell at $0.99)
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from assets import AssetConfig
from market import PolymarketClient
from notifier import send_telegram
from price_feed import BinanceWsPriceFeed
from signal_engine import Signal, SignalEngine

WINDOW_SECS = 300
DRY_RUN_BALANCE = 20.0
MIN_BET_USD = 1.0
BALANCE_RESERVE = 0.50
CSV_DIR = Path("data/sniper")

# V1 Kelly modes (mathematically grounded position sizing)
MODES = {
    "safe":       {"kelly_fraction": 0.25, "max_bet_pct": 0.25, "label": "Safe (¼ Kelly)"},
    "aggressive": {"kelly_fraction": 0.50, "max_bet_pct": 0.50, "label": "Aggressive (½ Kelly)"},
    "degen":      {"kelly_fraction": 1.00, "max_bet_pct": 1.00, "label": "Degen (full Kelly)"},
}


@dataclass
class WindowState:
    window_ts: int = 0
    slug: str = ""
    up_token: str = ""
    down_token: str = ""
    condition_id: str = ""
    open_price: float = 0.0
    best_signal: Signal = field(default_factory=Signal)
    prev_score: float = 0.0
    fired: bool = False
    fire_ts: float = 0.0
    fire_side: str = ""
    fire_token: str = ""
    fire_price: float = 0.0
    fire_shares: float = 0.0
    fire_confidence: float = 0.0
    order_id: str | None = None
    early_sold: bool = False
    early_sell_price: float = 0.0
    sell_attempts: int = 0


@dataclass
class Stats:
    windows: int = 0
    fired: int = 0
    skipped: int = 0
    wins: int = 0
    losses: int = 0
    early_exits: int = 0
    pnl: float = 0.0


class Sniper:
    def __init__(
        self,
        asset: AssetConfig,
        client: PolymarketClient,
        price_feed: BinanceWsPriceFeed,
        dry_run: bool = True,
        mode: str = "safe",
        max_bet: float = 50.0,
    ) -> None:
        self.asset = asset
        self.client = client
        self.price_feed = price_feed
        self.dry_run = dry_run
        self.mode_name = mode
        self.mode_cfg = MODES[mode]
        self.max_bet = max_bet
        self.engine = SignalEngine()
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        self._last_heartbeat_ts = 0.0
        self._last_reason = ""
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    # ── Timing ─────────────────────────────────────────────────────────────

    def _window_ts(self, now: float | None = None) -> int:
        now_ts = time.time() if now is None else now
        return int(now_ts - (now_ts % WINDOW_SECS))

    def _secs_left(self, now: float | None = None) -> float:
        now_ts = time.time() if now is None else now
        return self._window_ts(now_ts) + WINDOW_SECS - now_ts

    # ── V1 Kelly Criterion ─────────────────────────────────────────────────

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
            # Kelly says no edge at this token price.
            # For high-confidence signals with cheap tokens, use minimum bet
            # as override — the signal may still be profitable.
            if confidence >= 0.60 and token_price <= 0.85:
                return MIN_BET_USD
            return 0.0

        kelly *= self.mode_cfg["kelly_fraction"]
        cap = available * self.mode_cfg["max_bet_pct"]
        bet = min(available * kelly, cap, self.max_bet)
        return bet if bet >= MIN_BET_USD else 0.0

    # ── V2 Window Management ───────────────────────────────────────────────

    def _reset_window(self, window_ts: int, open_price: float) -> None:
        self.engine.reset()
        self.state = WindowState(window_ts=window_ts, open_price=open_price)
        self._last_reason = ""
        self.stats.windows += 1

    def _ensure_market(self) -> bool:
        if self.state.slug:
            return True
        market = self.client.find_market(self.asset.slug_prefix, self.state.window_ts)
        if not market:
            return False
        self.state.slug = market["slug"]
        self.state.condition_id = market.get("condition_id", "")
        self.state.up_token = market.get("up_token", "")
        self.state.down_token = market.get("down_token", "")
        return bool(self.state.up_token and self.state.down_token)

    # ── V2 Confirm Direction ───────────────────────────────────────────────

    def _confirm_direction(self, direction: str) -> bool:
        required = max(self.asset.confirm_ticks, 2)
        if len(self.engine.tick_prices) < required:
            return False
        recent = self.engine.tick_prices[-required:]
        deltas = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
        if direction == "UP":
            return all(d > 0 for d in deltas)
        if direction == "DOWN":
            return all(d < 0 for d in deltas)
        return False

    # ── V2 Should Fire (with all safety checks) ───────────────────────────

    def _should_fire(self, sig: Signal, secs_left: float) -> bool:
        if self.state.fired:
            self._last_reason = "already_fired"
            return False
        if not sig.direction:
            self._last_reason = "no_direction"
            return False
        # DOWN-only mode: UP trades are 4W/5L=-$8.78, DOWN are 8W/0L=+$9.30
        if sig.direction == "UP":
            self._last_reason = "up_disabled"
            return False
        if abs(sig.delta_pct) < self.asset.min_delta_pct:
            self._last_reason = f"delta<{self.asset.min_delta_pct}"
            return False
        # V2: direction must match delta sign
        if (sig.delta_pct > 0 and sig.direction != "UP") or (sig.delta_pct < 0 and sig.direction != "DOWN"):
            self._last_reason = f"dir_mismatch:{sig.direction}/delta={sig.delta_pct:+.4f}"
            return False
        if sig.confidence < self.asset.min_confidence:
            self._last_reason = f"conf<{self.asset.min_confidence}"
            return False
        if secs_left > self.asset.eval_start_secs or secs_left < self.asset.eval_end_secs:
            self._last_reason = f"outside_window:{secs_left:.0f}s"
            return False
        # V2: confirm consecutive ticks
        if not self._confirm_direction(sig.direction):
            self._last_reason = f"confirm<{self.asset.confirm_ticks}"
            return False
        self._last_reason = "READY"
        return True

    # ── Fire Trade (V2 structure + V1 GTC orders + V1 Kelly) ──────────────

    def _fire_trade(self, sig: Signal) -> bool:
        s = self.state
        a = self.asset
        token_id = s.up_token if sig.direction == "UP" else s.down_token
        if not token_id:
            print(f"[{a.name}] skip: market_not_found")
            return False

        # V2: spread check via typed Book
        book = self.client.fetch_book(token_id)
        print(
            f"[{a.name}] BOOK bid={book.best_bid:.3f} ask={book.best_ask:.3f} "
            f"spread={book.spread:.3f}"
        )
        if book.spread > a.max_spread:
            print(f"[{a.name}] skip: spread={book.spread:.3f} > max={a.max_spread}")
            return False
        if book.best_bid <= 0 or book.best_ask <= 0:
            print(f"[{a.name}] skip: empty book")
            return False

        buy_price = round(book.best_ask, 2)
        if buy_price > a.max_token_price or buy_price < a.min_token_price:
            print(f"[{a.name}] skip: price={buy_price:.2f} outside [{a.min_token_price}, {a.max_token_price}]")
            return False

        # V1: Kelly bet sizing
        bet = self._kelly_bet(sig.confidence, buy_price)
        if bet < MIN_BET_USD:
            print(f"[{a.name}] skip: Kelly no edge (conf={sig.confidence:.0%} price={buy_price:.2f})")
            return False

        shares = max(bet / buy_price, 5)
        cost = shares * buy_price
        roi = ((1.0 - buy_price) / buy_price) * 100
        sl = self._secs_left()

        print(
            f"\n[{a.name}] 🎯 FIRE: {sig.direction} @ {buy_price:.2f} x {shares:.0f}sh = ${cost:.2f}"
            f"\n[{a.name}]    Δ={sig.delta_pct:+.3f}% Conf={sig.confidence:.0%} "
            f"ROI={roi:.1f}% T-{sl:.0f}s"
        )

        # V1: GTC maker order
        order_id: str | None
        if self.dry_run:
            order_id = f"DRY-{a.name}-{sig.direction}-{s.window_ts}"
            print(f"[{a.name}] [DRY] Would MAKER BUY {sig.direction}")
        else:
            order_id = self.client.submit_maker_buy(
                token_id, buy_price, shares, f"{a.name}-{sig.direction}"
            )
            if not order_id:
                print(f"[{a.name}] [!] Order failed")
                return False
            bal = self.client.get_balance()
            if bal is not None:
                print(f"[{a.name}]    💰 Balance: ${bal:.2f}")

        s.fired = True
        s.fire_ts = time.time()
        s.fire_side = sig.direction
        s.fire_token = token_id
        s.fire_price = buy_price
        s.fire_shares = shares
        s.fire_confidence = sig.confidence
        s.order_id = order_id
        self.stats.fired += 1

        if not self.dry_run:
            send_telegram(
                f"🎯 {a.name}: {sig.direction} @ {buy_price:.2f} x {shares:.0f}sh = ${cost:.2f}\n"
                f"Δ={sig.delta_pct:+.3f}% Conf={sig.confidence:.0%} ROI={roi:.1f}% T-{sl:.0f}s\n"
                f"{self.mode_name}"
            )
        return True

    # ── Early Exit (V2 structure + V1 trigger threshold) ───────────────────

    def _maybe_early_exit(self) -> None:
        s = self.state
        if not s.fired or s.early_sold:
            return
        # Wait for GTC order to fill — needs time on Polymarket
        if time.time() - s.fire_ts < 30:
            return
        if s.sell_attempts >= 3:
            return

        token = s.fire_token
        book = self.client.fetch_book(token)
        cur_price = book.best_bid
        if cur_price <= 0:
            return

        gain = cur_price - s.fire_price
        if gain < self.asset.early_exit_profit:
            return

        # Sell at best_bid, clamped to Polymarket max 0.99
        sell_price = min(round(cur_price, 2), 0.99)
        if sell_price <= s.fire_price:
            sell_price = min(round(cur_price - 0.01, 2), 0.99)

        actual_gain = sell_price - s.fire_price
        profit = round(s.fire_shares * actual_gain * 0.98, 2)

        print(f"\n[{self.asset.name}] 💰 EARLY EXIT: sell @ {sell_price:.2f} "
              f"(bought {s.fire_price:.2f}) +${profit:.2f}")

        if not self.dry_run:
            s.sell_attempts += 1
            oid = self.client.submit_sell(
                token, sell_price, s.fire_shares,
                f"{self.asset.name}-{s.fire_side}-EARLY"
            )
            if not oid:
                return

        s.early_sold = True
        s.early_sell_price = sell_price
        self.stats.wins += 1
        self.stats.early_exits += 1
        self.stats.pnl += profit

        if not self.dry_run:
            send_telegram(f"💰 {self.asset.name} EARLY EXIT: {s.fire_side} +${profit:.2f}")

    # ── V1 Auto-sell winning tokens ────────────────────────────────────────

    def _auto_sell_winner(self, token_id: str) -> None:
        """After a WIN, sell tokens at $0.99 to recycle USDC back to balance.
        Based on gengar_polymarket_bot approach.
        """
        if self.dry_run:
            return

        time.sleep(5)  # wait for resolution to propagate

        # Refresh balance allowance for conditional token
        self.client.update_balance_allowance(token_id)

        sell_price = self.client.get_sell_price(token_id)
        if sell_price < 0.90:
            sell_price = 0.99  # fallback after resolution
        # Polymarket max price is 0.99
        sell_price = min(sell_price, 0.99)

        sell_id = self.client.submit_sell(
            token_id, sell_price, self.state.fire_shares,
            f"{self.asset.name}-{self.state.fire_side}-CLAIM"
        )
        if sell_id:
            print(f"[{self.asset.name}]    💰 Sold tokens → USDC recycled")
        else:
            print(f"[{self.asset.name}]    ⚠ Sell failed — check positions manually")

    # ── Result Resolution (V2 + V1 auto-sell) ─────────────────────────────

    def _finalize_previous_window(self, close_price: float) -> None:
        s = self.state
        a = self.asset
        if s.window_ts == 0:
            return
        if not s.fired:
            self.stats.skipped += 1
            return
        if s.early_sold:
            self._log_trade(
                result="EARLY_EXIT",
                resolved_side="EARLY_EXIT",
                close_price=close_price,
                pnl=(s.early_sell_price - s.fire_price) * s.fire_shares * 0.98,
            )
            return

        # V2: check Gamma API for resolution
        resolved_side = self.client.get_market_resolution(s.slug) if s.slug else None
        resolved_by = "market_winner"

        if not resolved_side:
            # V1 fallback: token midpoints
            up_mid = self.client.fetch_midpoint(s.up_token)
            down_mid = self.client.fetch_midpoint(s.down_token)
            if up_mid > 0.01 or down_mid > 0.01:
                resolved_side = "UP" if up_mid > down_mid else "DOWN"
                resolved_by = "token_midpoint"
            else:
                # Last fallback: Binance close price
                resolved_side = "UP" if close_price > s.open_price else "DOWN" if close_price < s.open_price else ""
                resolved_by = "reference_price"

        won = (s.fire_side == resolved_side) if resolved_side else False

        if won:
            profit = round(s.fire_shares * (1.0 - s.fire_price) * 0.98, 2)
            self.stats.wins += 1
            self.stats.pnl += profit
            result = "WIN"
            print(
                f"[{a.name}] ✅ WIN: {s.fire_side} | {s.fire_shares:.0f}sh @ ${s.fire_price:.2f} "
                f"→ +${profit:.2f} ({resolved_by})"
            )
            # V1: Auto-sell winning tokens to recycle USDC
            self._auto_sell_winner(s.fire_token)
        else:
            loss = round(s.fire_shares * s.fire_price, 2)
            self.stats.losses += 1
            self.stats.pnl -= loss
            profit = -loss
            result = "LOSS"
            print(
                f"[{a.name}] ❌ LOSS: {s.fire_side} | {s.fire_shares:.0f}sh @ ${s.fire_price:.2f} "
                f"→ -${loss:.2f} (resolved={resolved_side} by {resolved_by})"
            )

        # Show running stats
        bal = self.client.get_balance() if not self.dry_run else None
        bal_str = f"${bal:.2f}" if bal else "?"
        print(
            f"[{a.name}]    📊 W/L={self.stats.wins}/{self.stats.losses} "
            f"PnL=${self.stats.pnl:+.2f} Cash={bal_str}"
        )

        if not self.dry_run:
            send_telegram(
                f"{'✅' if won else '❌'} {a.name} {result}: {s.fire_side} ${profit:+.2f}\n"
                f"W/L={self.stats.wins}/{self.stats.losses} PnL=${self.stats.pnl:+.2f}"
            )

        self._log_trade(
            result=result,
            resolved_side=resolved_side or "",
            close_price=close_price,
            pnl=profit,
        )

    # ── CSV Logging (V2 DictWriter style) ──────────────────────────────────

    def _log_trade(self, *, result: str, resolved_side: str,
                   close_price: float, pnl: float) -> None:
        s = self.state
        path = CSV_DIR / f"trades_{self.asset.name.lower()}.csv"
        is_new = not path.exists()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "asset": self.asset.name,
            "slug": s.slug,
            "window_ts": s.window_ts,
            "mode": self.mode_name,
            "open_price": round(s.open_price, 4),
            "close_price": round(close_price, 4),
            "side": s.fire_side,
            "resolved_side": resolved_side,
            "result": result,
            "entry_price": round(s.fire_price, 2),
            "shares": round(s.fire_shares, 0),
            "cost": round(s.fire_shares * s.fire_price, 2),
            "confidence": round(s.fire_confidence, 2),
            "delta_pct": round(s.best_signal.delta_pct, 4),
            "score": round(s.best_signal.score, 2),
            "early_sold": int(s.early_sold),
            "pnl": round(pnl, 2),
            "cum_pnl": round(self.stats.pnl, 2),
            "order_id": s.order_id or "",
        }
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    # ── V2 Main Loop (clean step() architecture) ──────────────────────────

    def step(self, now: float | None = None) -> None:
        now_ts = time.time() if now is None else now

        # Read price from WebSocket (RAM read, ~0ms)
        if self.price_feed.is_stale(3000):
            self._last_reason = "stale_price"
            return
        snap = self.price_feed.latest()
        price = snap.price
        if price <= 0:
            return

        current_window = self._window_ts(now_ts)

        # First tick — initialize
        if self.state.window_ts == 0:
            self._reset_window(current_window, price)

        # New window — finalize previous, reset
        if current_window != self.state.window_ts:
            prev_close = (
                self.engine.tick_prices[-1]
                if self.engine.tick_prices
                else self.state.open_price or price
            )
            self._finalize_previous_window(prev_close)
            self._reset_window(current_window, price)

        self.engine.add_tick(price, now_ts)
        sig = self.engine.analyze(self.state.open_price, price)
        if abs(sig.score) > abs(self.state.best_signal.score):
            self.state.best_signal = sig
        secs_left = self._secs_left(now_ts)

        # Heartbeat log (every 10s)
        if now_ts - self._last_heartbeat_ts >= 10:
            self._last_heartbeat_ts = now_ts
            t_start = datetime.fromtimestamp(self.state.window_ts, timezone.utc).strftime("%H:%M")
            t_end = datetime.fromtimestamp(self.state.window_ts + WINDOW_SECS, timezone.utc).strftime("%H:%M")
            d = "▲" if sig.direction == "UP" else "▼" if sig.direction == "DOWN" else "━"
            print(
                f"[{self.asset.name}] {t_start}-{t_end} {d} ${price:,.2f} "
                f"Δ={sig.delta_pct:+.3f}% S={sig.score:+.1f} C={sig.confidence:.0%} "
                f"T-{secs_left:.0f}s | {self._last_reason}"
            )

        # Try to fire
        if self._ensure_market() and self._should_fire(sig, secs_left):
            print(
                f"\n[{self.asset.name}] 🔥 TRIGGER: {sig.direction} "
                f"Δ={sig.delta_pct:+.3f}% C={sig.confidence:.0%} T-{secs_left:.0f}s"
            )
            self._fire_trade(sig)

        self.state.prev_score = sig.score

        # Check early exit opportunity
        if self.state.fired and secs_left > max(self.asset.eval_end_secs - 2, 2):
            self._maybe_early_exit()

    def run(self) -> None:
        a = self.asset
        mode_label = "LIVE" if not self.dry_run else "DRY"
        bal = self.client.get_balance()
        bal_s = f"${bal:.2f}" if bal else "n/a"

        print(f"\n{'─' * 60}")
        print(f"  🎯 {a.name} Sniper V3 — {mode_label} {self.mode_name}")
        print(f"  Δ ≥ {a.min_delta_pct:.3f}% | Conf ≥ {a.min_confidence:.0%}"
              f" | Token [{a.min_token_price:.2f}, {a.max_token_price:.2f}]")
        print(f"  Eval T-{a.eval_start_secs}→T-{a.eval_end_secs}"
              f" | Spread ≤ {a.max_spread} | Confirm {a.confirm_ticks} ticks")
        print(f"  Price: WebSocket | Orders: GTC Maker | Sizing: Kelly"
              f" | Bal: {bal_s}")
        print(f"{'─' * 60}")

        self.running = True
        while self.running:
            self.step()
            time.sleep(0.5)  # V1 polling rate (faster than V2's 2.0s)

    def summary(self) -> str:
        s = self.stats
        wr = s.wins / (s.wins + s.losses) * 100 if (s.wins + s.losses) > 0 else 0
        return (
            f"{self.asset.name}: {s.fired} trades W/L={s.wins}/{s.losses} ({wr:.0f}%) "
            f"early={s.early_exits} PnL=${s.pnl:+.2f}"
        )
