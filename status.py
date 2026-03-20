"""
Portfolio status — real data from Polymarket APIs.
Shows: cash balance, open positions, recent trades, P&L.

Usage:
  python status.py              # full dashboard
  python status.py --trades 5   # last 5 trades
"""

import argparse
import httpx
import config

CLOB_HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

WALLET = config.POLY_FUNDER_ADDRESS.lower()


def get_balance(client: httpx.Client) -> dict:
    """Get USDC cash balance from CLOB."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        clob = ClobClient(
            host=CLOB_HOST,
            key=config.POLY_PRIVATE_KEY,
            chain_id=137,
            signature_type=1,
            funder=config.POLY_FUNDER_ADDRESS,
        )
        creds = clob.create_or_derive_api_creds()
        if creds:
            clob.set_api_creds(creds)
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = clob.get_balance_allowance(params)
        if isinstance(resp, dict):
            raw = float(resp.get("balance", 0) or 0)
            bal = raw / 1e6 if raw > 10_000 else raw
            return {"cash": bal}
    except Exception as e:
        return {"cash": 0, "error": str(e)}
    return {"cash": 0}


def get_positions(client: httpx.Client) -> list:
    """Get open positions from Polymarket Data API."""
    try:
        r = client.get(f"{DATA_API}/positions",
                       params={"user": WALLET}, timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("positions", data.get("data", []))
    except Exception:
        pass
    return []


def get_trades(client: httpx.Client, limit: int = 20) -> list:
    """Get recent trade history from Polymarket Data API."""
    try:
        r = client.get(f"{DATA_API}/activity",
                       params={"user": WALLET, "limit": limit},
                       timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("activity", data.get("data", []))
    except Exception:
        pass
    return []


def print_dashboard(n_trades: int = 10):
    client = httpx.Client(timeout=15.0)

    print(f"\n{'═' * 60}")
    print(f"  POLYMARKET PORTFOLIO — {WALLET[:10]}...{WALLET[-6:]}")
    print(f"{'═' * 60}")

    # Balance
    bal = get_balance(client)
    cash = bal.get("cash", 0)
    print(f"\n  💰 Cash: ${cash:.2f} USDC")
    if "error" in bal:
        print(f"     ⚠ {bal['error']}")

    # Positions
    positions = get_positions(client)
    if positions:
        print(f"\n  📊 Open Positions ({len(positions)}):")
        print(f"  {'Market':<40} {'Side':<6} {'Shares':>7} {'Avg':>6} {'Value':>8} {'P&L':>8}")
        print(f"  {'─' * 40} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 8} {'─' * 8}")

        total_value = 0
        total_cost = 0
        for pos in positions:
            # Handle different API response formats
            title = (pos.get("title", "") or pos.get("market", {}).get("question", ""))[:38]
            outcome = pos.get("outcome", pos.get("side", "?"))
            size = float(pos.get("size", 0) or pos.get("shares", 0) or 0)
            avg_price = float(pos.get("avgPrice", 0) or pos.get("avg_price", 0) or 0)
            cur_price = float(pos.get("curPrice", 0) or pos.get("current_price", 0) or 0)
            value = size * cur_price
            cost = size * avg_price
            pnl = value - cost

            total_value += value
            total_cost += cost

            pnl_str = f"${pnl:+.2f}" if cost > 0 else "—"
            val_str = f"${value:.2f}" if value > 0 else "$0.00"
            print(f"  {title:<40} {outcome:<6} {size:>7.1f} ${avg_price:>5.2f} {val_str:>8} {pnl_str:>8}")

        total_pnl = total_value - total_cost
        print(f"  {'─' * 40} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 8} {'─' * 8}")
        print(f"  {'TOTAL':<40} {'':6} {'':7} {'':6} ${total_value:>7.2f} ${total_pnl:>+7.2f}")
        print(f"\n  Portfolio: ${cash + total_value:.2f} (cash ${cash:.2f} + positions ${total_value:.2f})")
    else:
        print(f"\n  📊 No open positions")
        print(f"\n  Portfolio: ${cash:.2f}")

    # Recent trades
    trades = get_trades(client, limit=n_trades)
    if trades:
        print(f"\n  📜 Recent Trades ({len(trades)}):")
        print(f"  {'Time':<20} {'Type':<5} {'Outcome':<6} {'Price':>6} {'Shares':>7} {'Cost':>8} {'Market':<30}")
        print(f"  {'─' * 20} {'─' * 5} {'─' * 6} {'─' * 6} {'─' * 7} {'─' * 8} {'─' * 30}")

        for t in trades[:n_trades]:
            ts = t.get("timestamp", t.get("time", ""))
            if isinstance(ts, (int, float)):
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(ts, timezone.utc).strftime("%m/%d %H:%M")
            elif isinstance(ts, str) and len(ts) > 16:
                ts = ts[5:16]

            side = t.get("type", t.get("side", "?"))[:4]
            outcome = t.get("outcome", t.get("asset", "?"))[:5]
            price = float(t.get("price", 0) or 0)
            size = float(t.get("size", 0) or t.get("shares", 0) or 0)
            cost = price * size
            title = (t.get("title", "") or t.get("market", ""))[:28]

            print(f"  {ts:<20} {side:<5} {outcome:<6} ${price:>5.2f} {size:>7.1f} ${cost:>7.2f} {title}")
    else:
        print(f"\n  📜 No recent trades found")

    print(f"\n{'═' * 60}\n")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket portfolio status")
    parser.add_argument("--trades", type=int, default=10, help="Number of recent trades")
    args = parser.parse_args()
    print_dashboard(args.trades)
