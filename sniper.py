"""Core sniper engine."""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from assets import AssetConfig
from market import PolymarketClient
from notifier import send_telegram
from signal_engine import Signal, SignalEngine

WINDOW_SECS = 300
DRY_RUN_BALANCE = 20.0
MIN_BET_USD = 1.0
BALANCE_RESERVE = 0.50
CSV_DIR = Path("data/sniper")

MODES = {
    "safe": {"risk_scale": 0.25, "max_bet_pct": 0.18, "label": "Safe"},
    "aggressive": {"risk_scale": 0.50, "max_bet_pct": 0.30, "label": "Aggressive"},
    "degen": {"risk_scale": 1.00, "max_bet_pct": 0.45, "label": "Degen"},
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
    fire_side: str = ""
    fire_token: str = ""
    fire_price: float = 0.0
    fire_shares: float = 0.0
    fire_confidence: float = 0.0
    fire_stake_usd: float = 0.0
    order_id: str | None = None
    early_sold: bool = False
    early_sell_price: float = 0.0
    early_sell_order_id: str | None = None


@dataclass
class Stats:
    windows: int = 0
    fired: int = 0
    skipped: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    early_exits: int = 0
    closed: int = 0
    pnl: float = 0.0


class Sniper:
    def __init__(
        self,
        asset: AssetConfig,
        client: PolymarketClient,
        dry_run: bool = True,
        mode: str = "safe",
        max_bet: float = 50.0,
    ) -> None:
        self.asset = asset
        self.client = client
        self.dry_run = dry_run
        self.mode_name = mode
        self.mode_cfg = MODES[mode]
        self.max_bet = max_bet
        self.engine = SignalEngine()
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    def _window_ts(self, now: float | None = None) -> int:
        now_ts = time.time() if now is None else now
        return int(now_ts - (now_ts % WINDOW_SECS))

    def _secs_left(self, now: float | None = None) -> float:
        now_ts = time.time() if now is None else now
        return self._window_ts(now_ts) + WINDOW_SECS - now_ts

    def _balance(self) -> float:
        bal = self.client.get_balance()
        if self.dry_run and (bal is None or bal < MIN_BET_USD):
            return DRY_RUN_BALANCE
        return bal or 0.0

    def _stake_usd(self, confidence: float, token_price: float) -> float:
        bal = self._balance()
        available = max(bal - BALANCE_RESERVE, 0.0)
        if available < MIN_BET_USD:
            return 0.0
        if token_price <= 0 or token_price >= 0.99:
            return 0.0

        edge_proxy = max(confidence - self.asset.min_confidence, 0.0)
        if edge_proxy <= 0:
            return 0.0

        payout_ratio = max((1.0 - token_price) / max(token_price, 1e-6), 0.0)
        payout_scale = min(max(payout_ratio / 0.6, 0.50), 1.35)
        raw_fraction = (0.06 + edge_proxy * 0.45) * self.mode_cfg["risk_scale"] * payout_scale
        capped_fraction = min(raw_fraction, self.mode_cfg["max_bet_pct"])
        stake = min(self.max_bet, available * capped_fraction)
        return round(stake if stake >= MIN_BET_USD else 0.0, 2)

    def _reset_window(self, window_ts: int, open_price: float) -> None:
        self.engine.reset()
        self.state = WindowState(window_ts=window_ts, open_price=open_price)
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

    def _confirm_direction(self, direction: str) -> bool:
        required = max(self.asset.confirm_ticks, 2)
        if len(self.engine.tick_prices) < required:
            return False
        recent = self.engine.tick_prices[-required:]
        deltas = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
        if direction == "UP":
            return all(delta > 0 for delta in deltas)
        if direction == "DOWN":
            return all(delta < 0 for delta in deltas)
        return False

    def _should_fire(self, sig: Signal, secs_left: float) -> bool:
        if self.state.fired:
            return False
        if not sig.direction:
            return False
        if secs_left > self.asset.eval_start_secs or secs_left < self.asset.eval_end_secs:
            return False
        if abs(sig.delta_pct) < self.asset.min_delta_pct:
            return False
        if sig.confidence < self.asset.min_confidence:
            return False
        if not self._confirm_direction(sig.direction):
            return False
        return True

    def _entry_token(self, direction: str) -> str:
        return self.state.up_token if direction == "UP" else self.state.down_token

    def _fire_trade(self, sig: Signal) -> bool:
        token_id = self._entry_token(sig.direction)
        if not token_id:
            return False

        book = self.client.fetch_book(token_id)
        if book.spread > self.asset.max_spread:
            return False

        price = self.client.get_buy_price(
            token_id,
            max_price=self.asset.max_token_price,
            min_price=self.asset.min_token_price,
        )
        if price <= 0:
            return False

        stake = self._stake_usd(sig.confidence, price)
        if stake <= 0:
            return False
        shares = round(stake / price, 4)
        if shares <= 0:
            return False

        order_id = "dry-run"
        if not self.dry_run:
            order_id = self.client.submit_maker_buy(token_id, price, shares, f"{self.asset.name} {sig.direction}")
            if not order_id:
                return False

        self.state.fired = True
        self.state.fire_side = sig.direction
        self.state.fire_token = token_id
        self.state.fire_price = price
        self.state.fire_shares = shares
        self.state.fire_confidence = sig.confidence
        self.state.fire_stake_usd = stake
        self.state.order_id = order_id
        self.stats.fired += 1

        msg = (
            f"🎯 {self.asset.name} {sig.direction} | entry={price:.3f} | shares={shares:.4f} | "
            f"stake=${stake:.2f} | conf={sig.confidence:.2f} | delta={sig.delta_pct:.4f}%"
        )
        print(msg)
        send_telegram(msg)
        return True

    def _maybe_early_exit(self) -> None:
        if not self.state.fired or self.state.early_sold:
            return
        bid = self.client.get_sell_price(self.state.fire_token)
        if bid <= 0:
            return
        if bid - self.state.fire_price < self.asset.early_exit_profit:
            return

        order_id = "dry-run"
        if not self.dry_run:
            order_id = self.client.submit_sell(
                self.state.fire_token,
                bid,
                self.state.fire_shares,
                f"{self.asset.name} {self.state.fire_side}",
            )
            if not order_id:
                return

        self.state.early_sold = True
        self.state.early_sell_price = bid
        self.state.early_sell_order_id = order_id
        pnl = (bid - self.state.fire_price) * self.state.fire_shares
        self.stats.pnl += pnl
        self.stats.closed += 1
        self.stats.early_exits += 1
        if pnl > 0:
            self.stats.wins += 1
        elif pnl < 0:
            self.stats.losses += 1
        else:
            self.stats.flats += 1
        text = f"💸 {self.asset.name} early-exit @ {bid:.3f} | pnl=${pnl:+.2f}"
        print(text)
        send_telegram(text)

    def _reference_resolution(self, close_price: float) -> str:
        if close_price > self.state.open_price:
            return "UP"
        if close_price < self.state.open_price:
            return "DOWN"
        return "FLAT"

    def _log_trade(
        self,
        *,
        resolved_side: str,
        close_price: float,
        exit_price: float,
        result: str,
        resolved_by: str,
        pnl: float,
    ) -> None:
        path = CSV_DIR / f"{self.asset.slug_prefix}.csv"
        is_new = not path.exists()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "asset": self.asset.name,
            "slug": self.state.slug,
            "window_ts": self.state.window_ts,
            "open_price": round(self.state.open_price, 8),
            "close_price": round(close_price, 8),
            "side": self.state.fire_side,
            "resolved_side": resolved_side,
            "result": result,
            "resolved_by": resolved_by,
            "entry_price": round(self.state.fire_price, 4),
            "exit_price": round(exit_price, 4),
            "shares": round(self.state.fire_shares, 4),
            "stake_usd": round(self.state.fire_stake_usd, 4),
            "confidence": round(self.state.fire_confidence, 4),
            "order_id": self.state.order_id or "",
            "early_sold": int(self.state.early_sold),
            "pnl": round(pnl, 6),
        }
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    def _finalize_previous_window(self, close_price: float) -> None:
        if self.state.window_ts == 0:
            return

        if not self.state.fired:
            self.stats.skipped += 1
            return

        if self.state.early_sold:
            self._log_trade(
                resolved_side="EARLY_EXIT",
                close_price=close_price,
                exit_price=self.state.early_sell_price,
                result="EARLY_EXIT",
                resolved_by="best_bid",
                pnl=(self.state.early_sell_price - self.state.fire_price) * self.state.fire_shares,
            )
            return

        resolved_side = self.client.get_market_resolution(self.state.slug) if self.state.slug else None
        resolved_by = "market_winner" if resolved_side else "reference_price"
        if not resolved_side:
            resolved_side = self._reference_resolution(close_price)

        if resolved_side == "FLAT":
            exit_price = self.client.fetch_midpoint(self.state.fire_token) or self.client.get_sell_price(self.state.fire_token)
            pnl = (exit_price - self.state.fire_price) * self.state.fire_shares if exit_price > 0 else 0.0
            self.stats.flats += 1
            result = "FLAT"
        elif resolved_side == self.state.fire_side:
            exit_price = 1.0
            pnl = (1.0 - self.state.fire_price) * self.state.fire_shares
            self.stats.wins += 1
            result = "WIN"
        else:
            exit_price = 0.0
            pnl = -self.state.fire_price * self.state.fire_shares
            self.stats.losses += 1
            result = "LOSS"

        self.stats.closed += 1
        self.stats.pnl += pnl
        self._log_trade(
            resolved_side=resolved_side,
            close_price=close_price,
            exit_price=exit_price,
            result=result,
            resolved_by=resolved_by,
            pnl=pnl,
        )
        send_telegram(
            f"📌 {self.asset.name} {result} | side={self.state.fire_side} | resolved={resolved_side} | pnl=${pnl:+.2f}"
        )

    def step(self, now: float | None = None) -> None:
        now_ts = time.time() if now is None else now
        price = self.client.fetch_price(self.asset.binance_symbol)
        if price <= 0:
            return
        current_window = self._window_ts(now_ts)

        if self.state.window_ts == 0:
            self._reset_window(current_window, price)

        if current_window != self.state.window_ts:
            prev_close_price = self.engine.tick_prices[-1] if self.engine.tick_prices else self.state.open_price or price
            self._finalize_previous_window(prev_close_price)
            self._reset_window(current_window, price)

        self.engine.add_tick(price, now_ts)
        sig = self.engine.analyze(self.state.open_price, price)
        if sig.score > self.state.best_signal.score:
            self.state.best_signal = sig
        secs_left = self._secs_left(now_ts)

        if self._ensure_market() and self._should_fire(sig, secs_left):
            self._fire_trade(sig)
        self.state.prev_score = sig.score

        if self.state.fired and secs_left > max(self.asset.eval_end_secs - 2, 2):
            self._maybe_early_exit()

    def run(self) -> None:
        self.running = True
        while self.running:
            self.step()
            time.sleep(2.0)

    def summary(self) -> str:
        return (
            f"{self.asset.name}: windows={self.stats.windows} fired={self.stats.fired} "
            f"closed={self.stats.closed} early_exits={self.stats.early_exits} skipped={self.stats.skipped} "
            f"wins={self.stats.wins} losses={self.stats.losses} flats={self.stats.flats} "
            f"pnl=${self.stats.pnl:+.2f}"
        )
