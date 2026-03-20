# Fixed build notes

## What changed
- Rewrote `market.py` so token-price endpoints are used with Polymarket token IDs, not Binance symbols.
- Corrected midpoint parsing to use `mid_price`.
- Switched sniper execution from resting `GTC` logic to immediate `FOK` market-order logic.
- Rewrote `sniper.py` to remove pseudo-Kelly sizing, add a no-trade zone, executable-price early exit, and safer window finalization.
- Reworked `bot.py` so each thread owns its own client instead of sharing one client across assets.
- Added optional-import behavior for `py-clob-client`, so dry-run imports still work even if the SDK is missing.
- Kept helper scripts simple and non-deceptive: `set_allowances.py` now states clearly that allowance approval is onchain and not a CLOB-only toggle.

## What was verified locally
- `python -m py_compile *.py`
- `python smoke_test.py`

## What was not verified here
- Live order placement against Polymarket
- Real allowance approval flow
- Real Gamma/CLOB responses in your environment

Those need your API credentials, wallet setup, network access, and installed `py-clob-client`.
