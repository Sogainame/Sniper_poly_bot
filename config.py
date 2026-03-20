"""Environment configuration for the bot."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))

POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER_ADDRESS = os.getenv("POLY_FUNDER_ADDRESS", "")
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))
POLL_INTERVAL_SECS = float(os.getenv("POLL_INTERVAL_SECS", "2.0"))
