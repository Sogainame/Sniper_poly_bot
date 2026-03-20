"""Debug: check balance from all available sources."""
import httpx
import config

http = httpx.Client(timeout=10.0)
funder = config.POLY_FUNDER_ADDRESS
print(f"Funder address: {funder}")

# 1. Polymarket data API
print("\n─── Polymarket data API ───")
try:
    r = http.get("https://data-api.polymarket.com/value",
                  params={"user": funder.lower()})
    print(f"  {r.text[:300]}")
except Exception as e:
    print(f"  Error: {e}")

# 2. Gamma profiles API
print("\n─── Gamma profiles API ───")
try:
    r = http.get(f"https://gamma-api.polymarket.com/profiles/{funder.lower()}")
    print(f"  Status: {r.status_code}")
    print(f"  {r.text[:500]}")
except Exception as e:
    print(f"  Error: {e}")

# 3. CLOB derive + balance
print("\n─── CLOB client ───")
try:
    from py_clob_client.client import ClobClient
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=config.POLY_PRIVATE_KEY,
        chain_id=137, signature_type=1,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
    print(f"  Creds: {type(creds)} = {creds}")
    if creds:
        client.set_api_creds(creds)
        bal = client.get_balance_allowance()
        print(f"  Balance: {bal}")
except Exception as e:
    print(f"  Error: {e}")

# 4. On-chain
print("\n─── On-chain USDC ───")
for addr, label in [
    ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e"),
    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC native"),
]:
    try:
        data_hex = "0x70a08231" + funder.lower().replace("0x","").zfill(64)
        r = http.post("https://polygon-rpc.com",
            json={"jsonrpc":"2.0","method":"eth_call","id":1,
                  "params":[{"to":addr,"data":data_hex},"latest"]}, timeout=10)
        raw = int(r.json().get("result","0x0"), 16)
        print(f"  {label}: ${raw/1e6:.6f}")
    except Exception as e:
        print(f"  {label}: {e}")

# 5. CTF Exchange (where Polymarket actually holds deposited funds)
print("\n─── CTF Exchange (Polymarket deposit contract) ───")
ctf_exchange = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
try:
    data_hex = "0x70a08231" + funder.lower().replace("0x","").zfill(64)
    r = http.post("https://polygon-rpc.com",
        json={"jsonrpc":"2.0","method":"eth_call","id":1,
              "params":[{"to":ctf_exchange,"data":data_hex},"latest"]}, timeout=10)
    raw = int(r.json().get("result","0x0"), 16)
    print(f"  CTF balance: ${raw/1e6:.6f} (raw={raw})")
except Exception as e:
    print(f"  Error: {e}")
