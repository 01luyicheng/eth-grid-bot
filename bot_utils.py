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

import time
from datetime import datetime, timezone

def write_heartbeat(heartbeat_file):
    """Write heartbeat timestamp."""
    try:
        os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
        with open(heartbeat_file, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"Failed to write heartbeat: {e}")

def check_is_alive(heartbeat_file, max_age=180):
    """Return True if bot wrote a heartbeat within max_age seconds."""
    try:
        with open(heartbeat_file) as f:
            return time.time() - float(f.read().strip()) < max_age
    except Exception:
        return False

def write_log(msg, log_file, heartbeat_file, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(log_file, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Failed to write log: {e}")
    write_heartbeat(heartbeat_file)
