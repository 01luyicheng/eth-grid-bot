import os
import requests

_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

def telegram_notify(message, priority="INFO"):
    """Send alert via Telegram Bot. Silent failure if not configured."""
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return
    text = f"[{priority}] ETH-Trader-Bot\n{message}"
    url  = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        requests.post(url, json={"chat_id": _TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"Telegram notification failed: {e}")
