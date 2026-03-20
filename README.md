# Sniper Poly Bot

Multi-asset 5-minute market sniper for Polymarket BTC/ETH/SOL/XRP/DOGE Up/Down markets.

## Strategy

Based on research of real trading results and open-source bots:

- **Composite signal engine**: window delta (weight 6) + micro momentum (2) + acceleration (1.5) + tick consistency (1.5)
- **Maker GTC orders**: 0% taker fee + maker rebates
- **Kelly criterion**: position sizing based on edge and confidence
- **Per-asset tuning**: each coin has individual delta/confidence thresholds based on observed volatility and liquidity
- **Eval window**: BTC T-60→T-20, others T-150..170→T-30
- **Result check**: Polymarket token midpoints (Chainlink resolution source), not Binance
- **Early exit**: sells position if token price rises +$0.10 before window closes

## Asset Profiles

| Asset | Priority | Min Delta | Min Confidence | Binance Symbol | Notes |
|-------|----------|-----------|----------------|----------------|-------|
| BTC   | ⭐⭐⭐   | 0.02%     | 30%            | BTCUSDT        | Best liquidity, most bots |
| SOL   | ⭐⭐     | 0.03%     | 35%            | SOLUSDT        | Good, more volatile |
| ETH   | ⭐       | 0.04%     | 45%            | ETHUSDT        | Noisy, higher threshold |
| XRP   | ⚠️       | 0.05%     | 50%            | XRPUSDT        | Jerky moves, careful |
| DOGE  | ⚠️       | 0.08%     | 55%            | DOGEUSDT       | Meme, thin liquidity |

## Usage

```bash
# Single asset - DRY RUN
python bot.py --asset btc
python bot.py --asset sol
python bot.py --asset eth

# Single asset - LIVE
python bot.py --asset btc --live
python bot.py --asset btc --live --mode aggressive

# Multi-asset - run all configured assets
python bot.py --asset all

# Options
python bot.py --asset btc --live --mode degen --max-bet 20
```

## Modes

- `safe` — Quarter Kelly, max 25% of balance per trade
- `aggressive` — Half Kelly, max 50% of balance per trade  
- `degen` — Full Kelly, 100% of balance per trade

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
```

## Files

```
├── bot.py              # Entry point + CLI
├── sniper.py           # Core sniper engine (asset-agnostic)
├── signal_engine.py    # Composite signal analysis
├── assets.py           # Per-asset configs (thresholds, symbols)
├── market.py           # Polymarket API (market lookup, orders, prices)
├── notifier.py         # Telegram notifications
├── config.py           # Env loader
├── requirements.txt
├── .env.example
└── data/sniper/        # CSV trade logs per asset
```

## Environment Variables

```
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```
