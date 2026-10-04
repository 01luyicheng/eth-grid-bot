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

import eth_abi

class NonceManager:
    def __init__(self, rpc_fn, wallet):
        self.rpc    = rpc_fn
        self.wallet = wallet
        self._nonce = None
        self._lock  = threading.Lock()

    def get(self):
        with self._lock:
            if self._nonce is None:
                self._nonce = int(
                    self.rpc("eth_getTransactionCount", [self.wallet, "pending"])
                    ["result"], 16
                )
            nonce = self._nonce
            self._nonce += 1
            return nonce

    def confirm(self):
        """Called after a transaction is confirmed on-chain."""
        with self._lock:
            pass

    def rollback(self):
        """Called when a transaction fails — restore the pre-incremented nonce."""
        with self._lock:
            if self._nonce is not None and self._nonce > 0:
                self._nonce -= 1

def _exact_input_single(params):
    """
    Uniswap V3 exactInputSingle with flat ABI encoding.
    selector = 0x414bf389
    """
    selector = "414bf389"
    encoded = eth_abi.encode(
        ['address','address','uint24','address','uint256','uint256','uint256','uint160'],
        [
            params["token_in"],
            params["token_out"],
            params["fee"],
            params["recipient"],
            params["deadline"],
            params["amount_in"],
            params["amount_out_min"],
            params.get("sqrt_price_limit", 0),
        ]
    )
    return selector + encoded.hex()
