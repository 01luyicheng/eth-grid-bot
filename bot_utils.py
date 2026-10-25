import os
import time
import requests
import threading

def telegram_notify(message, priority="INFO"):
    """Send alert via Telegram Bot. Logs error on failure."""
    _TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return

    text = f"[{priority}] ETH-Trader\n{message}"
    url  = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(url, data={"chat_id": _TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Error sending telegram notification: {e}")


def _heartbeat_writer(heartbeat_file):
    """Write heartbeat timestamp every 60 seconds."""
    while True:
        try:
            os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
            with open(heartbeat_file, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            print(f"Error writing heartbeat: {e}")
        time.sleep(60)

def start_heartbeat(heartbeat_file):
    t = threading.Thread(target=_heartbeat_writer, args=(heartbeat_file,), daemon=True)
    t.start()

def is_alive(heartbeat_file, max_age=180):
    """Return True if bot wrote a heartbeat within max_age seconds."""
    try:
        with open(heartbeat_file) as f:
            return time.time() - float(f.read().strip()) < max_age
    except Exception as e:
        print(f"Error reading heartbeat: {e}")
        return False
