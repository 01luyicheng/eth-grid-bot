import os
import time
import json
import threading
from datetime import datetime, timezone
import requests

def telegram_notify(message, priority="INFO", bot_name="ETH-Trader"):
    """Send alert via Telegram Bot. Uses requests instead of urllib."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    text = f"[{priority}] {bot_name}\n{message}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"Telegram notification failed: {e}")


def _heartbeat_writer(heartbeat_file):
    """Write heartbeat timestamp every 60 seconds."""
    while True:
        try:
            os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
            with open(heartbeat_file, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            print(f"Heartbeat write failed to {heartbeat_file}: {e}")
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
        print(f"Failed to check heartbeat from {heartbeat_file}: {e}")
        return False


def setup_logger(log_file, heartbeat_file=None):
    def log(msg, level="INFO"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line)
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"Log write failed to {log_file}: {e}")

        if heartbeat_file:
            try:
                os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
                with open(heartbeat_file, "w") as f:
                    f.write(str(time.time()))
            except Exception as e:
                print(f"Heartbeat update from log failed: {e}")

    return log
