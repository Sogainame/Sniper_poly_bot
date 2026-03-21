#!/bin/bash
# Run: bash setup_env.sh
# Then edit .env and replace placeholder values with real ones

cat > ~/Sniper_poly_bot/.env << 'EOF'
POLY_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
POLY_FUNDER_ADDRESS=0xYOUR_PROXY_WALLET_HERE
POLY_SIGNATURE_TYPE=1
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
TELEGRAM_CHAT_ID=YOUR_CHAT_ID_HERE
POLL_INTERVAL_SECS=2.0
EOF

echo "✅ .env created at ~/Sniper_poly_bot/.env"
echo "Now edit it: nano ~/Sniper_poly_bot/.env"
