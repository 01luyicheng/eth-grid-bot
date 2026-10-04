import urllib.request
import urllib.parse
import time
import os
import threading

def telegram_notify(message, priority="INFO", bot_name="ETH-Trader"):
    """Send alert via Telegram Bot. Silent failure if not configured."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    text = f"[{priority}] {bot_name}\n{message}"
    url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):  # nosec B310
            pass
    except Exception as e:
        print(f"Telegram notify failed: {e}")

def _heartbeat_writer(heartbeat_file):
    """Write heartbeat timestamp every 60 seconds."""
    while True:
        try:
            os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
            with open(heartbeat_file, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            print(f"Heartbeat writer failed: {e}")
        time.sleep(60)

def start_heartbeat(heartbeat_file):
    t = threading.Thread(target=_heartbeat_writer, args=(heartbeat_file,), daemon=True)
    t.start()

def refresh_heartbeat(heartbeat_file):
    """Refresh heartbeat file."""
    try:
        os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
        with open(heartbeat_file, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"Heartbeat update failed: {e}")

def check_wallet_env_permissions(wallet_env_path):
    """Ensure wallet.env has strict 0o600 permissions."""
    st = os.stat(wallet_env_path)
    mode = st.st_mode & 0o777
    if mode != 0o600:
        raise RuntimeError(f"{wallet_env_path} permissions {oct(mode)} are too open; must be 0o600")
