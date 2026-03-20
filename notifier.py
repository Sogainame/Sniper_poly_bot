"""Telegram notifications."""

import httpx
import config


def send_telegram(message: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    if len(message) > 4000:
        message = message[:4000] + "\n... (truncated)"
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": message,
                  "disable_web_page_preview": True},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False
