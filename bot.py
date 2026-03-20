"""
Sniper Poly Bot — Multi-asset 5-minute Polymarket sniper.

Usage:
    python bot.py --asset btc                      # Single asset DRY
    python bot.py --asset btc --live               # Single asset LIVE
    python bot.py --asset all                      # All assets DRY
    python bot.py --asset btc,sol --live --mode aggressive
    python bot.py --asset all --live --mode degen --max-bet 10
"""

import argparse
import sys
import time
import threading

from assets import get_asset, get_enabled_assets, AssetConfig
from market import PolymarketClient
from sniper import Sniper
from notifier import send_telegram


def run_single(asset: AssetConfig, client: PolymarketClient,
               dry_run: bool, mode: str, max_bet: float):
    """Run sniper for a single asset (blocking)."""
    sniper = Sniper(asset, client, dry_run=dry_run, mode=mode, max_bet=max_bet)
    try:
        sniper.run()
    except KeyboardInterrupt:
        pass
    print(f"\n  {sniper.summary()}")
    return sniper


def run_multi(assets: list[AssetConfig], client: PolymarketClient,
              dry_run: bool, mode: str, max_bet: float):
    """Run snipers for multiple assets in separate threads."""
    snipers: list[Sniper] = []
    threads: list[threading.Thread] = []

    for asset in assets:
        s = Sniper(asset, client, dry_run=dry_run, mode=mode, max_bet=max_bet)
        snipers.append(s)
        t = threading.Thread(target=s.run, name=f"sniper-{asset.name}", daemon=True)
        threads.append(t)

    print(f"\n  Starting {len(assets)} snipers: {', '.join(a.name for a in assets)}")

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n  Stopping all snipers...")
        for s in snipers:
            s.running = False

    for t in threads:
        t.join(timeout=10)

    # Final summary
    print(f"\n{'=' * 64}")
    print("  📊 Session Summary")
    total_pnl = 0.0
    for s in snipers:
        print(f"  {s.summary()}")
        total_pnl += s.stats.pnl
    print(f"  TOTAL PnL: ${total_pnl:+.2f}")
    print(f"{'=' * 64}")

    send_telegram(
        "📊 Multi-asset session:\n"
        + "\n".join(s.summary() for s in snipers)
        + f"\nTOTAL: ${total_pnl:+.2f}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Sniper Poly Bot — Multi-asset 5-min Polymarket sniper"
    )
    parser.add_argument(
        "--asset", type=str, required=True,
        help="Asset(s): btc, sol, eth, xrp, doge, or 'all' or comma-separated (btc,sol)",
    )
    parser.add_argument("--live", action="store_true", help="LIVE mode")
    parser.add_argument(
        "--mode", choices=["safe", "aggressive", "degen"],
        default="safe", help="Trading mode (default: safe)",
    )
    parser.add_argument(
        "--max-bet", type=float, default=50.0,
        help="Max bet in USD per trade (default: 50)",
    )
    args = parser.parse_args()

    # Parse assets
    asset_str = args.asset.lower().strip()
    if asset_str == "all":
        assets = get_enabled_assets()
    else:
        names = [n.strip() for n in asset_str.split(",")]
        assets = [get_asset(n) for n in names]

    if not assets:
        print("No assets selected!")
        sys.exit(1)

    # Banner
    dry_run = not args.live
    label = "LIVE" if args.live else "DRY RUN"

    print("=" * 64)
    print(f"  🎯 Sniper Poly Bot — {label}")
    print(f"  Assets  : {', '.join(a.name for a in assets)}")
    print(f"  Mode    : {args.mode}")
    print(f"  Max bet : ${args.max_bet:.2f}")
    print("=" * 64)

    if args.live:
        print(f"\n  ⚠️  LIVE MODE — REAL MONEY!")
        print(f"  Ctrl+C to abort, or wait 5 seconds...\n")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("⛔ Aborted.")
            return

    # Shared client
    client = PolymarketClient()
    bal = client.get_balance()
    print(f"  Balance: ${bal:.2f}" if bal else "  Balance: n/a")

    # Run
    if len(assets) == 1:
        run_single(assets[0], client, dry_run, args.mode, args.max_bet)
    else:
        run_multi(assets, client, dry_run, args.mode, args.max_bet)

    print("\n⛔ Done")


if __name__ == "__main__":
    main()
