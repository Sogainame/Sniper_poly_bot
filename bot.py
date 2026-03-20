"""CLI entrypoint for the sniper bot."""
from __future__ import annotations

import argparse
import sys
import threading
import time

from assets import AssetConfig, get_asset, get_enabled_assets
from market import PolymarketClient
from notifier import send_telegram
from sniper import Sniper


def run_single(asset: AssetConfig, dry_run: bool, mode: str, max_bet: float) -> Sniper:
    sniper = Sniper(asset, PolymarketClient(), dry_run=dry_run, mode=mode, max_bet=max_bet)
    try:
        sniper.run()
    except KeyboardInterrupt:
        sniper.running = False
    print(sniper.summary())
    return sniper


def run_multi(assets: list[AssetConfig], dry_run: bool, mode: str, max_bet: float) -> None:
    snipers: list[Sniper] = []
    threads: list[threading.Thread] = []

    def worker(asset: AssetConfig) -> None:
        sniper = Sniper(asset, PolymarketClient(), dry_run=dry_run, mode=mode, max_bet=max_bet)
        snipers.append(sniper)
        sniper.run()

    for asset in assets:
        thread = threading.Thread(target=worker, name=f"sniper-{asset.slug_prefix}", args=(asset,), daemon=True)
        threads.append(thread)
        thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for sniper in snipers:
            sniper.running = False
        for thread in threads:
            thread.join(timeout=10)

    total_pnl = sum(s.stats.pnl for s in snipers)
    for sniper in snipers:
        print(sniper.summary())
    print(f"TOTAL PnL: ${total_pnl:+.2f}")
    send_telegram("\n".join(s.summary() for s in snipers) + f"\nTOTAL: ${total_pnl:+.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sniper Poly Bot")
    parser.add_argument(
        "--asset",
        type=str,
        required=True,
        help="btc | sol | eth | xrp | doge | all | comma-separated",
    )
    parser.add_argument("--live", action="store_true", help="Use live trading")
    parser.add_argument("--mode", choices=["safe", "aggressive", "degen"], default="safe")
    parser.add_argument("--max-bet", type=float, default=50.0)
    args = parser.parse_args()

    asset_arg = args.asset.lower().strip()
    if asset_arg == "all":
        assets = get_enabled_assets()
    else:
        assets = [get_asset(name.strip()) for name in asset_arg.split(",") if name.strip()]
    if not assets:
        print("No assets selected")
        sys.exit(1)

    dry_run = not args.live
    print("=" * 64)
    print(f"Sniper Poly Bot | mode={'LIVE' if args.live else 'DRY'} | assets={', '.join(a.name for a in assets)}")
    print(f"strategy={args.mode} | max_bet=${args.max_bet:.2f}")
    print("=" * 64)

    if len(assets) == 1:
        run_single(assets[0], dry_run=dry_run, mode=args.mode, max_bet=args.max_bet)
    else:
        run_multi(assets, dry_run=dry_run, mode=args.mode, max_bet=args.max_bet)


if __name__ == "__main__":
    main()
