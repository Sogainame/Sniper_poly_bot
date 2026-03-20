"""Debug: check balance from all available sources."""

import httpx
import config

http = httpx.Client(timeout=10.0)
funder = config.POLY_FUNDER_ADDRESS

print(f"Funder address: {funder}")
print()

# 1. Polymarket data API
print("─── Method 1: Polymarket data API ───")
try:
    r = http.get("https://data-api.polymarket.com/value",
                  params={"user": funder.lower()})
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text[:500]}")
except Exception as e:
    print(f"  Error: {e}")

print()

# 2. CLOB API
print("─── Method 2: CLOB balance_allowance ───")
try:
    from py_clob_client.client import ClobClient
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=config.POLY_PRIVATE_KEY,
        chain_id=137,
        signature_type=1,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    resp = client.get_balance_allowance()
    print(f"  Response: {resp}")
except Exception as e:
    print(f"  Error: {e}")

    # Try direct REST call with derived creds
    print("  Trying direct REST...")
    try:
        creds = client.get_api_creds()
        print(f"  Creds available: {bool(creds)}")
        if creds:
            headers = {
                "POLY_API_KEY": creds.get("apiKey", ""),
                "POLY_API_SECRET": creds.get("secret", ""),
                "POLY_PASSPHRASE": creds.get("passphrase", ""),
            }
            r = http.get("https://clob.polymarket.com/balance-allowance",
                         headers=headers, timeout=10.0)
            print(f"  REST Status: {r.status_code}")
            print(f"  REST Response: {r.text[:300]}")
    except Exception as e2:
        print(f"  REST Error: {e2}")

print()

# 3. Direct on-chain USDC
print("─── Method 3: On-chain USDC ───")
contracts = [
    ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e (bridged)"),
    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC (native)"),
]
for addr, label in contracts:
    try:
        data_hex = "0x70a08231" + funder.lower().replace("0x", "").zfill(64)
        r = http.post("https://polygon-rpc.com",
                      json={"jsonrpc": "2.0", "method": "eth_call", "id": 1,
                            "params": [{"to": addr, "data": data_hex}, "latest"]},
                      timeout=10.0)
        result = r.json().get("result", "0x0")
        raw = int(result, 16)
        bal = raw / 1e6
        print(f"  {label}: ${bal:.6f} (raw={raw})")
    except Exception as e:
        print(f"  {label}: Error: {e}")

print()
print("─── Summary ───")
print("If all show $0, your USDC might be deposited INTO Polymarket")
print("(locked in their smart contract, not in your wallet).")
print("Check polymarket.com → your portfolio balance.")
