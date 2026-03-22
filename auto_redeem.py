#!/usr/bin/env python3
"""Auto-redeem winning Polymarket positions via poly-web3 (Safe/Proxy)."""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
PROXY_WALLET = os.getenv("POLY_PROXY_WALLET") or os.getenv("PROXY_WALLET")
BUILDER_KEY = os.getenv("BUILDER_API_KEY") or os.getenv("BUILDER_KEY")
BUILDER_SECRET = os.getenv("BUILDER_SECRET")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE")

CHAIN_ID = 137
HOST = "https://clob.polymarket.com"

def main():
    # Validate env
    missing = []
    if not PRIVATE_KEY: missing.append("POLY_PRIVATE_KEY")
    if not PROXY_WALLET: missing.append("POLY_PROXY_WALLET")
    if not BUILDER_KEY: missing.append("BUILDER_API_KEY")
    if not BUILDER_SECRET: missing.append("BUILDER_SECRET")
    if not BUILDER_PASSPHRASE: missing.append("BUILDER_PASSPHRASE")
    if missing:
        print(f"[!] Missing env vars: {', '.join(missing)}")
        print("    Add them to .env")
        sys.exit(1)

    from py_clob_client.client import ClobClient
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from poly_web3 import RELAYER_URL, PolyWeb3Service

    # 1. Init CLOB client (signature_type=2 for Safe wallet)
    print(f"[*] Proxy wallet: {PROXY_WALLET}")
    clob = ClobClient(
        HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=1,  # Proxy wallet
        funder=PROXY_WALLET,
    )
    clob.set_api_creds(clob.create_or_derive_api_creds())
    print("[+] CLOB client ready")

    # 2. Init Relayer client
    relayer = RelayClient(
        RELAYER_URL,
        CHAIN_ID,
        PRIVATE_KEY,
        BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=BUILDER_KEY,
                secret=BUILDER_SECRET,
                passphrase=BUILDER_PASSPHRASE,
            )
        ),
    )
    print("[+] Relayer client ready")

    # 3. Init poly-web3 service
    service = PolyWeb3Service(
        clob_client=clob,
        relayer_client=relayer,
    )
    print("[+] PolyWeb3Service ready")

    # 4. Redeem all
    print("[*] Redeeming all positions...")
    results = service.redeem_all(batch_size=10)

    if not results:
        print("[=] No redeemable positions found")
    else:
        ok = sum(1 for r in results if r is not None)
        fail = sum(1 for r in results if r is None)
        print(f"[+] Done: {ok} redeemed, {fail} failed")
        for i, r in enumerate(results):
            if r is not None:
                print(f"    #{i+1}: {r}")
            else:
                print(f"    #{i+1}: FAILED (retry later)")

    # 5. Check balance
    try:
        from market import PolymarketClient
        c = PolymarketClient()
        bal = c.get_balance()
        print(f"\n[*] Balance: ${bal:.2f}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
