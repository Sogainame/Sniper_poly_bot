#!/usr/bin/env python3
"""Window observer — record BTC + token prices every 5s for N windows.

No trading. Pure data collection.
Output: data/observer/window_{ts}.csv per window + summary to stdout.

Usage:
    python3 observer.py                 # 10 windows (default)
    python3 observer.py --windows 20    # 20 windows
    python3 observer.py --interval 3    # poll every 3s
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from price_feed import BinanceWsPriceFeed

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
WINDOW_SECS = 300
OUT_DIR = Path("data/observer")


@dataclass
class Tick:
    t_minus: float        # seconds before window close
    btc_price: float
    btc_delta_pct: float  # vs window open
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float


def find_market(http: httpx.Client, window_ts: int) -> dict | None:
    slug = f"btc-updown-5m-{window_ts}"
    try:
        resp = http.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        markets = payload if isinstance(payload, list) else [payload]
        market = next((m for m in markets if m and m.get("slug") == slug), None)
        if not market:
            return None

        clob_ids = market.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        up_token = down_token = ""
        for i, name in enumerate(outcomes):
            if i >= len(clob_ids):
                break
            ln = str(name).lower()
            if ln in ("up", "yes"):
                up_token = clob_ids[i]
            elif ln in ("down", "no"):
                down_token = clob_ids[i]
        if not up_token and len(clob_ids) >= 1:
            up_token = clob_ids[0]
        if not down_token and len(clob_ids) >= 2:
            down_token = clob_ids[1]

        return {"slug": slug, "up_token": up_token, "down_token": down_token}
    except Exception as e:
        print(f"[!] Market lookup error: {e}")
        return None


def fetch_price(http: httpx.Client, token_id: str, side: str) -> float:
    try:
        resp = http.get(
            f"{CLOB_HOST}/price",
            params={"token_id": token_id, "side": side},
            timeout=5,
        )
        if resp.status_code == 200:
            return float(resp.json().get("price", 0))
    except Exception:
        pass
    return 0.0


def observe_window(
    http: httpx.Client,
    feed: BinanceWsPriceFeed,
    window_ts: int,
    interval: float,
) -> list[Tick]:
    close_time = window_ts + WINDOW_SECS
    market = find_market(http, window_ts)
    if not market:
        print(f"  [!] Market not found for {window_ts}")
        return []

    up_tok = market["up_token"]
    down_tok = market["down_token"]

    # Get open price
    snap = feed.latest()
    open_price = snap.price
    if open_price <= 0:
        print(f"  [!] No BTC price")
        return []

    ticks: list[Tick] = []

    t_start = datetime.fromtimestamp(window_ts, timezone.utc).strftime("%H:%M")
    t_end = datetime.fromtimestamp(close_time, timezone.utc).strftime("%H:%M")
    print(f"\n  Window {t_start}-{t_end} | open=${open_price:,.2f}")
    print(f"  {'T-sec':>6} {'BTC':>10} {'Δ%':>8} {'UP bid':>7} {'UP ask':>7} {'DN bid':>7} {'DN ask':>7}")
    print(f"  {'─'*58}")

    while True:
        now = time.time()
        secs_left = close_time - now
        if secs_left < -2:
            break

        snap = feed.latest()
        btc = snap.price
        if btc <= 0:
            time.sleep(interval)
            continue

        delta_pct = (btc - open_price) / open_price * 100

        up_bid = fetch_price(http, up_tok, "SELL")
        up_ask = fetch_price(http, up_tok, "BUY")
        down_bid = fetch_price(http, down_tok, "SELL")
        down_ask = fetch_price(http, down_tok, "BUY")

        tick = Tick(
            t_minus=round(secs_left, 1),
            btc_price=btc,
            btc_delta_pct=round(delta_pct, 4),
            up_bid=up_bid, up_ask=up_ask,
            down_bid=down_bid, down_ask=down_ask,
        )
        ticks.append(tick)

        # Direction arrow
        d = "▲" if delta_pct > 0.01 else "▼" if delta_pct < -0.01 else "━"
        print(
            f"  {secs_left:>5.0f}s {d} ${btc:>9,.2f} {delta_pct:>+7.3f}% "
            f"  {up_bid:.3f}  {up_ask:.3f}  {down_bid:.3f}  {down_ask:.3f}"
        )

        time.sleep(interval)

    # Determine result
    final_snap = feed.latest()
    final_price = final_snap.price
    result = "UP" if final_price >= open_price else "DOWN"
    final_delta = (final_price - open_price) / open_price * 100
    print(f"  ─── RESULT: {result} | close=${final_price:,.2f} Δ={final_delta:+.3f}%")

    return ticks


def save_window_csv(window_ts: int, ticks: list[Tick]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"window_{window_ts}.csv"
    fields = ["t_minus", "btc_price", "btc_delta_pct",
              "up_bid", "up_ask", "down_bid", "down_ask"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for t in ticks:
            writer.writerow({
                "t_minus": t.t_minus,
                "btc_price": round(t.btc_price, 2),
                "btc_delta_pct": t.btc_delta_pct,
                "up_bid": t.up_bid,
                "up_ask": t.up_ask,
                "down_bid": t.down_bid,
                "down_ask": t.down_ask,
            })
    return path


def main():
    parser = argparse.ArgumentParser(description="Window Observer")
    parser.add_argument("--windows", type=int, default=10)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Window Observer — {args.windows} windows, poll every {args.interval}s")
    print(f"  Recording: BTC price + UP/DOWN token bid/ask")
    print(f"  Output: {OUT_DIR}/window_*.csv")
    print("=" * 60)

    feed = BinanceWsPriceFeed("btcusdt")
    feed.start()
    print("[WS] Waiting for price...")
    for _ in range(50):
        if not feed.is_stale(5000):
            break
        time.sleep(0.1)
    snap = feed.latest()
    print(f"[WS] BTC: ${snap.price:,.2f}")

    http = httpx.Client(timeout=10)

    windows_done = 0
    try:
        while windows_done < args.windows:
            now = time.time()
            current_window = int(now - (now % WINDOW_SECS))
            next_window = current_window + WINDOW_SECS
            secs_to_next = next_window - now

            # If more than 30s into current window, observe it
            # Otherwise wait for next
            if secs_to_next > 270:
                # Just started a new window, observe it
                window_ts = current_window
            else:
                # Wait for next window
                print(f"\n  Waiting {secs_to_next:.0f}s for next window...")
                time.sleep(secs_to_next + 1)
                window_ts = next_window

            ticks = observe_window(http, feed, window_ts, args.interval)
            if ticks:
                path = save_window_csv(window_ts, ticks)
                print(f"  Saved: {path} ({len(ticks)} ticks)")
                windows_done += 1
                print(f"\n  [{windows_done}/{args.windows}] windows done")

    except KeyboardInterrupt:
        print(f"\n  Stopped. {windows_done} windows recorded.")

    feed.stop()
    http.close()
    print(f"\nData in: {OUT_DIR}/")


if __name__ == "__main__":
    main()
