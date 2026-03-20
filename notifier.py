"""Telegram notifications."""
from __future__ import annotations

import httpx

import config



def send_telegram(message: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    text = message if len(message) <= 4000 else message[:3995] + "\n..."
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=config.HTTP_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception:
        return False
