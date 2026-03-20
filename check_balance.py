"""Debug: check balance with fixed methods."""
import config

print(f"Funder: {config.POLY_FUNDER_ADDRESS}")
print(f"Key set: {bool(config.POLY_PRIVATE_KEY)}")

# Test 1: BalanceAllowanceParams fix (bug #83 workaround)
print("\n─── CLOB with BalanceAllowanceParams ───")
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=config.POLY_PRIVATE_KEY,
        chain_id=137, signature_type=1,
        funder=config.POLY_FUNDER_ADDRESS,
    )
    creds = client.create_or_derive_api_creds()
    print(f"  Creds: {creds}")
    if creds:
        client.set_api_creds(creds)
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params)
        print(f"  Balance response: {resp}")
        if isinstance(resp, dict):
            raw = float(resp.get("balance", 0) or 0)
            bal = raw / 1e6 if raw > 10_000 else raw
            print(f"  Parsed: ${bal:.6f}")
except Exception as e:
    print(f"  Error: {e}")

# Test 2: Direct REST with creds
print("\n─── Direct REST /balance-allowance ───")
try:
    import httpx
    api_key = getattr(creds, 'api_key', '')
    api_secret = getattr(creds, 'api_secret', '')
    api_passphrase = getattr(creds, 'api_passphrase', '')
    print(f"  api_key: {api_key[:12]}...")
    
    http = httpx.Client(timeout=10.0)
    headers = {
        "POLY_API_KEY": api_key,
        "POLY_API_SECRET": api_secret,
        "POLY_PASSPHRASE": api_passphrase,
    }
    r = http.get("https://clob.polymarket.com/balance-allowance",
                  params={"asset_type": "COLLATERAL", "signature_type": "1"},
                  headers=headers, timeout=10.0)
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text[:500]}")
except Exception as e:
    print(f"  Error: {e}")
