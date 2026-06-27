#!/usr/bin/env python3
"""
ETH/USDC Multi-Grid Auto-Trader on Base Chain
- 5-tier buy grid + 4-tier sell grid
- Trailing stop (3%, activates above $1900)
- Stop loss at $1600
- Checks every 30 seconds

BUG FIXES APPLIED (2026-06-18):
  [Bug#1]  GRID BUY: fixed double-swap; cooldown check moved before swap;
           hardcoded "$1760" replaced with actual level; usdc_needed calc unified.
  [Bug#2]  STOP LOSS: added explicit success checks for wrap+swap before return True;
           returns False and skips "COMPLETE!" log on any failure.
  [Issue#3] swap_usdc_for_weth(): added slippage_price param; callers pass level
            price so amount_out_min uses the intended grid-entry price.
  [Issue#4] approve(): grants infinite allowance (2**256-1) when amount_wei=None.
  [Issue#5] get_max_fee(): properly separates baseFee from priorityFee (EIP-1559).
  [Issue#6] build_tx(): get_gas_price()/get_max_fee() called once at top;
            no redundant get_gas_price() call inside get_max_fee().
  [Issue#7] BUY/SELL_LEVELS, SWAP_AMOUNT_WETH, TRADE_COOLDOWN now read from wallet.env
            with sensible defaults as fallback.
"""
import eth_abi, requests, json, time, os, signal, sys, fcntl, hmac, hashlib, secrets, stat, threading, urllib.request, urllib.parse
from web3 import Web3

# === wallet.env path ===
_WALLET_ENV = "/root/.openclaw/workspace/wallet/wallet.env"

# === Trading params (loaded from wallet.env, defaults as fallback) ===
# Defaults used when wallet.env doesn't specify the key
STOP_LOSS_DEFAULT         = 1600.0
SWAP_AMOUNT_WETH_DEFAULT  = 0.0002
TRADE_COOLDOWN_DEFAULT    = 120
CHECK_INTERVAL_DEFAULT    = 10

# === 网格动态计算参数 (Scheme B) ===
GRID_CENTER_DEFAULT         = "auto"
GRID_RANGE_PCT_DEFAULT      = 10
GRID_TIERS_DEFAULT           = 5
STOP_LOSS_BUFFER_PCT_DEFAULT = 5   # 网格下沿下方 5%

# [Fix#2] Check file permissions BEFORE reading private key
st = os.stat(_WALLET_ENV)
if stat.S_IMODE(st.st_mode) & 0o077:
    raise RuntimeError(f"{_WALLET_ENV} must be 0o600, found {oct(st.st_mode)}")

# Load from wallet.env (overrides defaults when present)
for line in open(_WALLET_ENV):
    line = line.strip()
    if line.startswith("PRIVATE_KEY="):
        PRIVATE_KEY = line.split("=", 1)[1].strip()
    elif line.startswith("WALLET_ADDRESS="):
        WALLET = line.split("=", 1)[1].strip()
    elif line.startswith("GRID_CENTER="):
        GRID_CENTER = line.split("=", 1)[1].split("#")[0].strip()
    elif line.startswith("GRID_RANGE_PCT="):
        GRID_RANGE_PCT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("GRID_TIERS="):
        GRID_TIERS = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("STOP_LOSS_BUFFER_PCT="):
        STOP_LOSS_BUFFER_PCT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("STOP_LOSS="):
        STOP_LOSS = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("SWAP_AMOUNT_WETH="):
        SWAP_AMOUNT_WETH = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("TRADE_COOLDOWN="):
        TRADE_COOLDOWN = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("CHECK_INTERVAL="):
        CHECK_INTERVAL = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("SLIPPAGE="):
        SLIPPAGE_TXT = line.split("=", 1)[1].split("#")[0].strip()
    elif line.startswith("TRAIL_TRIGGER_PRICE="):
        TRAIL_TRIGGER_PRICE = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("MIN_BALANCE_ETH="):
        MIN_BALANCE_ETH = float(line.split("=", 1)[1].split("#")[0].strip())
    # [VolGrid] New config parameters from wallet.env
    elif line.startswith("ENABLE_VOLATILITY_GRID="):
        ENABLE_VOLATILITY_GRID = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("ATR_LOOKBACK="):
        ATR_LOOKBACK = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("ATR_SPACING_MULT="):
        ATR_SPACING_MULT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("ATR_RANGE_MULT="):
        ATR_RANGE_MULT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("FALLBACK_RANGE_PCT="):
        FALLBACK_RANGE_PCT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("ENABLE_RSI_FILTER="):
        ENABLE_RSI_FILTER = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("RSI_PERIOD="):
        RSI_PERIOD = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("RSI_OVERSOLD="):
        RSI_OVERSOLD = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("RSI_OVERBOUGHT="):
        RSI_OVERBOUGHT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("ENABLE_GRID_DRIFT="):
        ENABLE_GRID_DRIFT = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("GRID_DRIFT_MAX_SHIFT_PCT="):
        GRID_DRIFT_MAX_SHIFT_PCT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("GRID_DRIFT_THRESHOLD_PCT="):
        GRID_DRIFT_THRESHOLD_PCT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("DYNAMIC_STOP_LOSS_ATR_MULT="):
        DYNAMIC_STOP_LOSS_ATR_MULT = float(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("CONSECUTIVE_LOSS_PAUSE="):
        CONSECUTIVE_LOSS_PAUSE = int(line.split("=", 1)[1].split("#")[0].strip())
    elif line.startswith("GAS_RESERVE_ETH="):
        GAS_RESERVE_ETH = float(line.split("=", 1)[1].split("#")[0].strip())

# [Fix#2] Lock down wallet.env AFTER permission check and loop end
os.chmod(_WALLET_ENV, 0o600)

# [VolGrid] Apply defaults for new parameters (keep all defaults off to preserve compatibility)
if "ENABLE_VOLATILITY_GRID" not in dir():  ENABLE_VOLATILITY_GRID  = 0
if "ATR_LOOKBACK"          not in dir():  ATR_LOOKBACK             = 14
if "ATR_SPACING_MULT"      not in dir():  ATR_SPACING_MULT         = 0.5
if "ATR_RANGE_MULT"       not in dir():  ATR_RANGE_MULT          = 2.0
if "FALLBACK_RANGE_PCT"    not in dir():  FALLBACK_RANGE_PCT      = 10.0
if "ENABLE_RSI_FILTER"     not in dir():  ENABLE_RSI_FILTER        = 0
if "RSI_PERIOD"            not in dir():  RSI_PERIOD              = 14
if "RSI_OVERSOLD"          not in dir():  RSI_OVERSOLD            = 35.0
if "RSI_OVERBOUGHT"        not in dir():  RSI_OVERBOUGHT          = 65.0
if "ENABLE_GRID_DRIFT"     not in dir():  ENABLE_GRID_DRIFT       = 0
if "GRID_DRIFT_MAX_SHIFT_PCT" not in dir(): GRID_DRIFT_MAX_SHIFT_PCT = 15.0
if "GRID_DRIFT_THRESHOLD_PCT" not in dir(): GRID_DRIFT_THRESHOLD_PCT = 3.0
if "DYNAMIC_STOP_LOSS_ATR_MULT" not in dir(): DYNAMIC_STOP_LOSS_ATR_MULT = 3.0
if "CONSECUTIVE_LOSS_PAUSE" not in dir(): CONSECUTIVE_LOSS_PAUSE = 3
if "GAS_RESERVE_ETH" not in dir(): GAS_RESERVE_ETH = 0.001

# [Fix#1] SLIPPAGE parsing: support both decimal ("0.5") and integer ("5") formats
if "SLIPPAGE_TXT" in dir():
    slip_val = float(SLIPPAGE_TXT)
    if slip_val < 0.01:           # already in 0.005 format
        SLIPPAGE = slip_val
    elif slip_val < 1:             # user wrote "0.5" meaning 0.5%
        SLIPPAGE = slip_val / 100
    else:                          # user wrote "5" meaning 5%
        SLIPPAGE = slip_val / 1000
else:
    SLIPPAGE = 0.005   # [Bug#1] 0.5% default; was hardcoded

# [Fix#2] Removed: os.chmod(_WALLET_ENV, 0o600) moved to after permission check above

# Apply defaults for any keys not found in wallet.env
if "STOP_LOSS" not in dir():        STOP_LOSS        = STOP_LOSS_DEFAULT
if "SWAP_AMOUNT_WETH" not in dir(): SWAP_AMOUNT_WETH = SWAP_AMOUNT_WETH_DEFAULT
if "TRADE_COOLDOWN" not in dir():   TRADE_COOLDOWN   = TRADE_COOLDOWN_DEFAULT
if "GRID_CENTER" not in dir():          GRID_CENTER          = GRID_CENTER_DEFAULT
if "GRID_RANGE_PCT" not in dir():      GRID_RANGE_PCT       = GRID_RANGE_PCT_DEFAULT
if "GRID_TIERS" not in dir():           GRID_TIERS           = GRID_TIERS_DEFAULT
if "STOP_LOSS_BUFFER_PCT" not in dir(): STOP_LOSS_BUFFER_PCT = STOP_LOSS_BUFFER_PCT_DEFAULT
# [Enhancement B] TRAIL_TRIGGER_PRICE default — was hardcoded at 1900
if "TRAIL_TRIGGER_PRICE" not in dir(): TRAIL_TRIGGER_PRICE = 1900.0
# [Enhancement C] MIN_BALANCE_ETH safety threshold
if "MIN_BALANCE_ETH" not in dir():    MIN_BALANCE_ETH    = 0.001

# === 运行时参数 ===
if "CHECK_INTERVAL" not in dir(): CHECK_INTERVAL = CHECK_INTERVAL_DEFAULT

# [Fix#7] Validate wallet.env values
if not PRIVATE_KEY or len(PRIVATE_KEY.strip()) < 64:
    raise RuntimeError(f"PRIVATE_KEY is missing or invalid in {_WALLET_ENV}")
if not WALLET or not WALLET.startswith("0x"):
    raise RuntimeError(f"WALLET_ADDRESS is missing or invalid in {_WALLET_ENV}")

# === [Fix#1] Independent HMAC key (NOT derived from PRIVATE_KEY) ===
_HMAC_KEY_FILE = "/root/.openclaw/workspace/wallet/state_hmac.key"
if os.path.exists(_HMAC_KEY_FILE):
    STATE_HMAC_KEY = open(_HMAC_KEY_FILE, "rb").read()
    # Re-check permissions on every load (in case they were changed)
    st_hmac = os.stat(_HMAC_KEY_FILE)
    if stat.S_IMODE(st_hmac.st_mode) & 0o077:
        raise RuntimeError(f"{_HMAC_KEY_FILE} permissions are too open; must be 0o600")
    if len(STATE_HMAC_KEY) < 32:
        raise RuntimeError(f"{_HMAC_KEY_FILE} key too short; regenerate")
else:
    STATE_HMAC_KEY = secrets.token_bytes(32)
    with open(_HMAC_KEY_FILE, "wb") as f:
        f.write(STATE_HMAC_KEY)
    os.chmod(_HMAC_KEY_FILE, 0o600)
    log(f"Generated new HMAC key: {_HMAC_KEY_FILE}")

# === Gas constants ===
PRIORITY_FEE = 500_000_000  # 0.5 gwei — fixed tip for EIP-1559

RPC = "https://mainnet.base.org"
PROXY = "http://127.0.0.1:10808"

# === Telegram Alerts ===
_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

def telegram_notify(message, priority="INFO"):
    """Send alert via Telegram Bot. Silent failure if not configured."""
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return
    text = f"[{priority}] ETH-Grid-Bot\n{message}"
    url  = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": _TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass  # silent failure

# DeFi addresses (Base mainnet)
WETH   = "0x4200000000000000000000000000000000000006"
USDC   = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

# Position sizing & intervals


# Slippage & trailing stop
# [Bug#1] SLIPPAGE is now loaded from wallet.env above (parsed with Fix#1 logic)
# [Enhancement B] TRAIL_TRIGGER_PRICE is now loaded from wallet.env above
TRAIL_PCT           = 0.03  # 3% trailing stop

# [Fix#3] Pending TX timeout (seconds)
PENDING_TX_TIMEOUT = 180  # 3 minutes

# State
# State — persist in workspace to survive reboots
_STATE_DIR  = "/root/.openclaw/workspace/eth-grid-bot/data"
STATE_FILE  = os.path.join(_STATE_DIR, "trade_state.json")
os.makedirs(_STATE_DIR, exist_ok=True)

LOG_FILE   = "/tmp/eth_trader.log"

session = requests.Session()
session.proxies = {"http": PROXY, "https": PROXY}
w3 = Web3()
acct = w3.eth.account.from_key(PRIVATE_KEY)

# [Fix#10] NonceManager — prevents race condition on concurrent txs
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
            pass  # nonce was pre-incremented on get(); nothing extra needed

    def rollback(self):
        """Called when a transaction fails — restore the pre-incremented nonce."""
        with self._lock:
            if self._nonce is not None and self._nonce > 0:
                self._nonce -= 1

nonce_mgr = NonceManager(rpc, WALLET)

# === Heartbeat (health check) ===
import threading as _heartbeat_thread
_HEARTBEAT_FILE = "/root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_auto"

def _heartbeat_writer():
    """Write heartbeat timestamp every 60 seconds."""
    while True:
        try:
            with open(_HEARTBEAT_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        time.sleep(60)

def start_heartbeat():
    t = _heartbeat_thread.Thread(target=_heartbeat_writer, daemon=True)
    t.start()

def is_alive(max_age=180):
    """Return True if bot wrote a heartbeat within max_age seconds."""
    try:
        with open(_HEARTBEAT_FILE) as f:
            return time.time() - float(f.read().strip()) < max_age
    except Exception:
        return False

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    # Refresh heartbeat on any log activity
    try:
        with open(_HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass

def rpc_with_retry(method, params=None, max_retries=3, base_delay=1):
    for attempt in range(max_retries):
        try:
            r = session.post(RPC, json={"jsonrpc":"2.0","method":method,"params":params or [],"id":1}, timeout=15)
            return r.json()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))

# [Fix#5] Alias rpc for backward compat; prefer rpc_with_retry
def rpc(method, params=None):
    return rpc_with_retry(method, params)

def get_nonce():
    return int(rpc("eth_getTransactionCount", [WALLET, "pending"])["result"], 16)

def get_gas_price():
    return int(rpc("eth_gasPrice")["result"], 16)

def get_max_fee():
    """EIP-1559: maxFeePerGas = (baseFee * 2) + priorityFee.
    get_gas_price() returns baseFee + priorityFee combined, so we back out
    priorityFee as a fixed ~0.5 gwei (Base typical) and recompute correctly."""
    combined = get_gas_price()   # = baseFee + priorityFee
    base_fee = combined - PRIORITY_FEE
    return base_fee * 2 + PRIORITY_FEE

def sign_send(tx, state=None):
    """Sign and send a transaction.
    [Enhancement A] If state dict is passed, records pending_tx for tracking."""
    try:
        signed = acct.sign_transaction(tx)
        hex_tx = "0x" + signed.raw_transaction.hex()
        result = rpc("eth_sendRawTransaction", [hex_tx])
        if "result" in result:
            tx_hash = result["result"]
            log(f"  TX: {tx_hash[:20]}...")
            # [Enhancement A] Track pending tx so we know what's in-flight
            if state is not None:
                state["pending_tx"] = tx_hash
                state["pending_tx_sent"] = time.time()
            return tx_hash
        elif "error" in result:
            log(f"  Error: {result['error'].get('message','')[:80]}")
            return None
    except Exception as e:
        log(f"  Sign error: {e}")
        # [Fix#10] Rollback nonce on failure so next tx can reuse it
        nonce_mgr.rollback()
        return None

def wait_tx(tx_hash, timeout=90):
    start = time.time()
    while time.time() - start < timeout:
        result = rpc("eth_getTransactionReceipt", [tx_hash])
        receipt = result.get("result")
        if receipt:
            ok = receipt.get("status") == "0x1"
            if not ok:
                log(f"  TX FAILED! Gas used: {receipt.get('gasUsed','?')}")
            return ok
        time.sleep(3)
    return False

def build_tx(to, data, gas, value=0):
    # [Issue#6] Call once — avoid redundant get_gas_price() / get_max_fee()
    max_fee = get_max_fee()
    nonce   = nonce_mgr.get()  # [Fix#10] Use NonceManager
    return {
        "from": WALLET,
        "to": to,
        "data": data,
        "value": value,
        "nonce": nonce,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": PRIORITY_FEE,  # [Fix#1] fixed tip, not combined gas_price
        "chainId": 8453,
        "type": 2,
        "gas": gas,
    }

# === Balance helpers ===
def get_eth():
    return int(rpc("eth_getBalance", [WALLET, "latest"])["result"], 16) / 1e18

def get_token_balance(token):
    data = "0x70a08231" + WALLET[2:].lower().zfill(64)
    r = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    result = r.get("result") or "0x0"
    return int(result, 16)

def get_weth():   return get_token_balance(WETH) / 1e18
def get_usdc():  return get_token_balance(USDC) / 1e6

def get_allowance(token, spender):
    data = "0xdd62ed3e" + WALLET[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    r = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    result = r.get("result") or "0x0"
    return int(result, 16)

# [VolGrid] Indicator caching (updated hourly)
last_indicator_update = 0
atr_cache = None
rsi_cache = None

# === Price (with fallback + sanity check) ===
def get_eth_price(state=None):
    price = None
    sources_used = []
    try:
        r = session.get(
            "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=ETH_USDT",
            timeout=10
        )
        price = float(r.json()[0]["last"])
        sources_used.append("Gate.io")
    except Exception as e:
        log(f"Price fetch error (Gate.io): {e}")
    if price is None:
        try:
            r = session.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
                timeout=10
            )
            price = float(r.json()["price"])
            sources_used.append("Binance")
        except Exception as e:
            log(f"Price fetch error (Binance): {e}")
    if price is None:
        return None
    # [Fix#8] Sanity check: reject price deviating >15% from last tick
    if state is not None and state.get("last_price") is not None:
        last = state["last_price"]
        max_deviation = last * 0.15
        if abs(price - last) > max_deviation:
            log(f"  ⚠️ Price {price:.2f} deviates >15% from last {last:.2f}, using previous price")
            price = last
    log(f"  ETH price: ${price:.2f} (source: {', '.join(sources_used)})")
    return price

# === OHLCV data from Gate.io (uses rpc_with_retry for robustness) ===
def get_ohlcv_gateio(interval="1h", limit=50):
    try:
        url = (f"https://api.gateio.ws/api/v4/spot/candlesticks"
               f"?currency_pair=ETH_USDT&interval={interval}&limit={limit}")
        r = session.get(url, timeout=10)
        r.raise_for_status()
        raw = r.json()
        if not isinstance(raw, list):
            log(f"[VolGrid] get_ohlcv_gateio: unexpected response type {type(raw)}")
            return []
        result = []
        for item in raw:
            if len(item) >= 8:
                ts = int(item[0])
                o  = float(item[2])
                h  = float(item[4])
                l  = float(item[5])
                c  = float(item[3])
                v  = float(item[1])
                result.append((ts, o, h, l, c, v))
        log(f"[VolGrid] get_ohlcv_gateio: fetched {len(result)} candles")
        return result
    except Exception as e:
        log(f"[VolGrid] get_ohlcv_gateio error: {e}")
        return []

# [VolGrid] === ATR computation ===
def compute_atr(ohlcv, period=14):
    """
    Average True Range.
    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = simple moving average of TR over `period` candles.
    Requires at least `period` candles.
    Returns float or None if insufficient data.
    """
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        _, _, high_prev, low_prev, close_prev, _ = ohlcv[i - 1]
        _, _, high_cur,  low_cur,  close_cur,  _  = ohlcv[i]
        tr = max(high_cur - low_cur, abs(high_cur - close_prev), abs(low_cur - close_prev))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

# [VolGrid] === RSI computation ===
def compute_rsi(closes, period=14):
    """
    Relative Strength Index (standard Wilder smoothing).
    Returns float in [0, 100] or None if insufficient data.
    """
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100.0

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

# [VolGrid] === Volatility-adaptive grid levels ===
def compute_volatility_grid_levels(center_price, atr, tiers, spacing_mult, range_mult):
    # [Fix#6] Guard against zero/negative ATR
    if atr is None or atr <= 0:
        log(f"  [VolGrid] ATR={atr} invalid, skipping volatility grid")
        return None, None
    """
    Build symmetric buy/sell grids sized by current volatility.
    total_range = atr * range_mult   # total price width
    spacing     = atr * spacing_mult  # distance between tiers
    bottom      = center_price - total_range / 2
    """
    total_range = atr * range_mult
    spacing     = atr * spacing_mult
    bottom      = center_price - total_range / 2

    buy_levels  = [round(bottom + spacing * i, 2) for i in range(1, tiers)]
    sell_levels = [round(bottom + spacing * (tiers - 1 + i), 2) for i in range(1, tiers)]

    log(f"[VOLA-GRID] ATR=${atr:.2f} range=${total_range:.2f} spacing=${spacing:.2f} "
        f"BUY={[round(x, 2) for x in buy_levels]} SELL={[round(x, 2) for x in sell_levels]}")

    return buy_levels, sell_levels

# === Trade actions ===
def wrap_eth(amount_wei, state=None):
    log(f"  Wrapping {amount_wei/1e18:.6f} ETH → WETH")
    tx = build_tx(WETH, "0xd0e30db0", gas=70000, value=amount_wei)
    h = sign_send(tx, state=state)
    return wait_tx(h) if h else False

# [Fix#4] Poll allowance until approved (up to 30s), instead of fixed sleep
def wait_approval(token, amount_wei, spender=ROUTER, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        allowance = get_allowance(token, spender)
        if allowance >= amount_wei:
            return True
        time.sleep(3)
    return False

def approve(token, spender, amount_wei=None):
    # [Issue#4] Infinite approval (2**256-1) when amount_wei is None
    amount = amount_wei if amount_wei is not None else (2**256 - 1)
    amount_str = "unlimited" if amount_wei is None else (f"{amount_wei/1e6:.2f}" if token == USDC else f"{amount_wei/1e18:.6f}")
    log(f"  Approving {spender[:10]} for {amount_str}...")
    data = (
        "0x095ea7b3"
        + spender[2:].lower().zfill(64)
        + hex(amount)[2:].zfill(64)
    )
    tx = build_tx(token, data, gas=50000)
    h = sign_send(tx)
    return wait_tx(h) if h else False

# === Dynamic grid computation (Scheme B) ===
def compute_grid_levels(center_price, range_pct, tiers):
    """
    Generate symmetric uniform grid levels around a center price.
    BUY  = from grid_bottom up to (but not including) center — tiers prices
    SELL = from just above center up to grid_top — tiers-1 prices
    No overlap between buy and sell zones.
    """
    half_range = center_price * (range_pct / 100)
    step = half_range / (tiers - 1)          # step per side only, not full range
    # BUY: bottom → (center - step), all strictly below center, ascending
    buy_levels  = sorted([
        round(center_price - half_range + step * i, 2) for i in range(tiers - 1)
    ])
    # SELL: (center + step) → top, ascending
    sell_levels = sorted([
        round(center_price + step * i, 2) for i in range(1, tiers)
    ])
    return buy_levels, sell_levels

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

def swap_weth_for_usdc(amount_wei, price, state=None):
    """Sell WETH → USDC with slippage protection"""
    amount_out_min = int(amount_wei * price * (1 - SLIPPAGE))
    log(f"  Swap {amount_wei/1e18:.6f} WETH → USDC (min {amount_out_min/1e6:.2f})")
    calldata = _exact_input_single({
        "token_in": WETH,
        "token_out": USDC,
        "fee": 3000,
        "recipient": WALLET,
        "deadline": int(time.time()) + 600,
        "amount_in": amount_wei,
        "amount_out_min": amount_out_min,
        "sqrt_price_limit": 0,
    })
    tx = build_tx(ROUTER, "0x" + calldata, gas=200000)
    h = sign_send(tx, state=state)
    return wait_tx(h) if h else False

def swap_usdc_for_weth(amount_usdc_wei, price, slippage_price=None, state=None):
    """Buy WETH ← USDC with slippage protection.
    [Issue#3] slippage_price allows caller to pass the grid level price as
    reference, so amount_out_min is based on the intended entry price rather
    than the (potentially worse) current market price."""
    ref_price = slippage_price if slippage_price else price
    amount_out_min = int(amount_usdc_wei / ref_price * (1 - SLIPPAGE))
    log(f"  Swap {amount_usdc_wei/1e6:.2f} USDC → WETH (min {amount_out_min/1e18:.6f})")
    # Check/pre-approve allowance
    allowance = get_allowance(USDC, ROUTER)
    log(f"  Allowance: {allowance/1e6:.4f} USDC vs needed {amount_usdc_wei/1e6:.2f}")
    if allowance < amount_usdc_wei:
        # [Fix#6] Approve with 10% buffer to avoid edge-case insufficient allowance
        approve_amount = int(amount_usdc_wei * 1.1)
        log(f"  → Approving {approve_amount/1e6:.2f} USDC...")
        ok = approve(USDC, ROUTER, approve_amount)
        if not ok:
            log(f"  APPROVE FAILED")
            return False
        # [Fix#4] Poll for approval confirmation instead of fixed sleep
        if not wait_approval(USDC, approve_amount, ROUTER, timeout=30):
            log("  Approval wait timeout, proceeding anyway")
    else:
        log(f"  Router allowed, skipping approve")
    calldata = _exact_input_single({
        "token_in": USDC,
        "token_out": WETH,
        "fee": 3000,
        "recipient": WALLET,
        "deadline": int(time.time()) + 600,
        "amount_in": amount_usdc_wei,
        "amount_out_min": amount_out_min,
        "sqrt_price_limit": 0,
    })
    tx = build_tx(ROUTER, "0x" + calldata, gas=200000)
    h = sign_send(tx, state=state)
    if not h:
        log(f"  SEND FAILED: sign_send returned None")
        return False
    log(f"  TX sent, waiting...")
    result = wait_tx(h)
    log(f"  Confirmation: {result}")
    # [Enhancement A] Clear pending_tx on confirmation
    if state is not None:
        state["pending_tx"] = None
    return result

def liquidate_all(price, state=None):
    """Emergency stop loss: sell all ETH/WETH → USDC.
    [Bug#2] Explicit swap_done flag; COMPLETE! only printed after successful
    swap (or when there is genuinely nothing to liquidate).
    [Enhancement A] state param lets us record pending_tx."""
    total_pos = get_eth() + get_weth()
    if total_pos <= GAS_RESERVE_ETH:
        log(f"  Balance {total_pos:.6f} <= gas reserve, skipping liquidate")
        return True
    sellable = total_pos - GAS_RESERVE_ETH
    log(f"  STOP LOSS: liquidating {sellable:.6f} ETH at ${price:.2f}")

    wrap_ok   = True   # True if no wrap was needed, or wrap succeeded
    swap_done = False # True only if we actually attempted a WETH→USDC swap
    swap_ok   = False

    # Step 1: wrap all ETH
    eth_bal = get_eth()
    if eth_bal > GAS_RESERVE_ETH:
        wrap_ok = wrap_eth(int((eth_bal - GAS_RESERVE_ETH) * 1e18), state=state)
        if not wrap_ok:
            log(f"  Stop loss ABORTED: wrap failed")
            return False
        # [Fix#4] Poll for wrap tx confirmation — removed invalid wait_approval call (Fix#5)

    # Step 2: swap all WETH → USDC
    weth_bal = get_weth()
    if weth_bal > 0:
        swap_done = True
        swap_ok   = swap_weth_for_usdc(int(weth_bal * 1e18), price, state=state)
        if not swap_ok:
            log(f"  Stop loss PARTIAL: swap failed, ETH may be stuck as WETH")
            return False

    # [Bug#2] Only print COMPLETE if we actually did work
    if swap_done:
        log(f"  Stop loss COMPLETE!")
    # [Enhancement A] Clear pending tx on successful liquidation
    if state is not None:
        state["pending_tx"] = None
    return True

# === State management ===
def default_state():
    return {
        "last_trade": 0,
        "last_price": None,
        "alerts_sent": {},
        "start_time": time.time(),
        "buy_triggered": {},    # {level: True}
        "sell_triggered": {},   # {level: True}
        "trail_armed": False,
        "trail_peak": None,     # float price
        # [Enhancement A] Pending tx tracking
        "pending_tx": None,     # tx_hash of last sent transaction
        "pending_tx_sent": 0.0, # timestamp when pending_tx was sent
        # [Fix#8] Trailing stop re-entry protection
        "pending_liquidation": False,  # True while liquidation tx is pending
        # [VolGrid] Indicator and grid tracking
        "atr": None,             # Latest ATR value
        "rsi": None,             # Latest RSI value
        "grid_center": None,     # Current grid center (for drift)
        "atr_last_update": 0,   # Timestamp of last ATR update
        # [Fix#2] Stop-loss cooldown + re-arm
        "stop_loss_triggered": False,
        "stop_loss_cooldown_until": 0.0,
        # [Fix#1] Separate flag so stop-loss can fire even when grid tx is pending
        "pending_stop_loss": False,
        # [Fix#11] Consecutive loss counter for risk control
        "consecutive_loss": 0,
        # [Fix#6] Trailing stop state + cooldown
        "trail_triggered": False,
        "trail_cooldown_until": 0.0,
    }

def _state_sign(data):
    """Compute HMAC-SHA256 signature for state data (Fix#3)."""
    return hmac.new(STATE_HMAC_KEY, json.dumps(data, sort_keys=True).encode(), hashlib.sha256).hexdigest()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
            # [Fix#3] Verify HMAC integrity before accepting state
            saved_sig = raw.pop("_hmac", None)
            if saved_sig is not None:
                expected = _state_sign(raw)
                if saved_sig != expected:
                    log("FATAL: State file tampered, resetting state")
                    return default_state()
            s = default_state()
            s.update(raw)
            return s
        except Exception as e:
            log(f"State load error: {e}, using defaults")
    return default_state()

def save_state(state):
    # Ensure data dir exists (idempotent)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # [Fix#3] Attach HMAC signature before writing
    state_to_save = dict(state)
    state_to_save["_hmac"] = _state_sign(state)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state_to_save, f, indent=2)
    os.rename(tmp, STATE_FILE)  # atomic

def should_alert(state, key, msg, cooldown=14400):
    now = time.time()
    last = state["alerts_sent"].get(key, 0)
    if now - last > cooldown:
        log(f"🚨 {msg}")
        state["alerts_sent"][key] = now
        return True
    return False

# === Main loop ===
def main():
    # [Bug#2] Single-instance lock — prevents nonce conflicts from concurrent runs
    LOCK_FILE = "/tmp/eth_trader.lock"
    lock_f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance already running. Exiting.")
        sys.exit(1)

    log("=== Multi-Grid ETH Auto-Trader STARTED ===")
    log(f"Wallet: {WALLET}")
    telegram_notify(f"🚀 ETH Grid Bot started\nPrice: ${init_price:.2f}\nGrid: {BUY_LEVELS[0]:.0f}~{SELL_LEVELS[-1]:.0f}", "INFO")
    log(f"Grid center: {GRID_CENTER} | Range: ±{GRID_RANGE_PCT}% | Tiers: {GRID_TIERS}")
    log(f"Stop-loss buffer: {STOP_LOSS_BUFFER_PCT}% | Swap: {SWAP_AMOUNT_WETH} ETH | Check: {CHECK_INTERVAL}s")
    # [VolGrid] Log new feature states
    log(f"[VolGrid] Volatility grid: {'ON' if ENABLE_VOLATILITY_GRID else 'OFF'} | "
        f"RSI filter: {'ON' if ENABLE_RSI_FILTER else 'OFF'} | Drift: {'ON' if ENABLE_GRID_DRIFT else 'OFF'} | "
        f"DynSL-ATR-mult: {DYNAMIC_STOP_LOSS_ATR_MULT}")
    log(f"📡 Heartbeat: /root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_auto")
    log(f"   Check: /root/.openclaw/bin/check_bot_alive.sh [max_age] [auto|hf]")
    start_heartbeat()

    # [Scheme B] Compute dynamic grid levels from parameters
    if GRID_CENTER == "auto":
        # [Fix#8] Pass state for price sanity check on init
        init_price = get_eth_price(state=state)
        if init_price is None:
            log("FATAL: cannot fetch price for auto-grid, exiting")
            sys.exit(1)
        grid_center = init_price
    else:
        grid_center = float(GRID_CENTER)

    BUY_LEVELS, SELL_LEVELS = compute_grid_levels(grid_center, GRID_RANGE_PCT, GRID_TIERS)

    # Compute dynamic STOP_LOSS from grid bottom
    STOP_LOSS = round(BUY_LEVELS[0] * (1 - STOP_LOSS_BUFFER_PCT / 100), 2)

    log(f"[AUTO-GRID] Center=${grid_center:.2f} Range=±{GRID_RANGE_PCT}% Tiers={GRID_TIERS}")
    log(f"[AUTO-GRID] BUY  = {[round(x, 2) for x in BUY_LEVELS]}")
    log(f"[AUTO-GRID] SELL = {[round(x, 2) for x in SELL_LEVELS]}")
    log(f"[AUTO-GRID] STOP_LOSS = ${STOP_LOSS:.2f} (${STOP_LOSS_BUFFER_PCT}% below grid bottom ${BUY_LEVELS[0]:.2f})")
    # [Fix#4] Dynamic trailing stop trigger — set 2% above grid top
    TRAIL_TRIGGER_PRICE = round(SELL_LEVELS[-1] * 1.02, 2)
    log(f"[AUTO-GRID] TRAIL_TRIGGER_PRICE = ${TRAIL_TRIGGER_PRICE:.2f} (dynamic, 2% above grid top ${SELL_LEVELS[-1]:.2f})")

    state = load_state()

    # Log restored state
    if state["buy_triggered"] or state["sell_triggered"] or state["trail_armed"]:
        log(f"[RESTORE] buy={list(state['buy_triggered'].keys())} sell={list(state['sell_triggered'].keys())} trail_armed={state['trail_armed']}")

    # [VolGrid] Track init_grid_center separately from state (state grid_center drifts, init stays fixed)
    init_grid_center = state.get("grid_center") or grid_center
    grid_center = init_grid_center   # mutable ref for drift tracking

    while True:
        try:
            # [Fix#8] Pass state for price sanity check
            price = get_eth_price(state=state)
            if price is None:
                log("Price unavailable, retrying in 30s")
                time.sleep(CHECK_INTERVAL)
                continue

            eth_bal = get_eth()
            weth_bal = get_weth()
            usdc_bal = get_usdc()
            total_usd = eth_bal * price + weth_bal * price + usdc_bal

            # [VolGrid] Set `now` BEFORE pending_tx check (fixes reference-before-assignment bug)
            now = time.time()

            # ===== [VolGrid] Indicator update (hourly) =====
            atr = state.get("atr")
            rsi = state.get("rsi")
            if now - state.get("atr_last_update", 0) > 3600:
                ohlcv = get_ohlcv_gateio(interval="1h", limit=max(ATR_LOOKBACK + 5, 50))
                closes = [c for _, _, _, _, c, _ in ohlcv]
                raw_atr = compute_atr(ohlcv, ATR_LOOKBACK) if len(ohlcv) >= ATR_LOOKBACK + 1 else None
                raw_rsi = compute_rsi(closes, RSI_PERIOD) if len(closes) >= RSI_PERIOD + 1 else None
                if raw_atr is not None:
                    atr = raw_atr
                    log(f"[VolGrid] ATR updated: ${atr:.2f}")
                if raw_rsi is not None:
                    rsi = raw_rsi
                    log(f"[VolGrid] RSI updated: {rsi:.1f}")
                state["atr"] = atr
                state["rsi"] = rsi
                state["atr_last_update"] = now

            log(f"Price: ${price:.2f} | ETH: {eth_bal:.6f} | USDC: ${usdc_bal:.2f} | Total: ${total_usd:.2f}"
                + (f" | ATR: ${atr:.2f}" if atr else "")
                + (f" | RSI: {rsi:.1f}" if rsi else ""))

            # ===== [VolGrid] RSI direction filter =====
            skip_buy  = False
            skip_sell = False
            if ENABLE_RSI_FILTER and rsi is not None:
                if rsi < RSI_OVERSOLD:
                    skip_buy = True
                    log(f"[RSI] RSI={rsi:.1f} < {RSI_OVERSOLD} — skipping BUY orders")
                if rsi > RSI_OVERBOUGHT:
                    skip_sell = True
                    log(f"[RSI] RSI={rsi:.1f} > {RSI_OVERBOUGHT} — skipping SELL orders")

            # ===== [VolGrid] Grid recomputation: volatility-adaptive vs drift (mutually exclusive) =====
            # Priority: volatility grid (if enabled + ATR available) > grid drift > periodic re-center
            effective_grid_center = grid_center
            grid_recomputed = False

            if ENABLE_VOLATILITY_GRID and atr is not None and atr > 0:
                # [VolGrid] Volatility-adaptive grid: center = current price, ATR-sized bands
                effective_grid_center = price
                grid_center = price
                vol_buy, vol_sell = compute_volatility_grid_levels(
                    price, atr, GRID_TIERS, ATR_SPACING_MULT, ATR_RANGE_MULT)
                if vol_buy and vol_sell:
                    BUY_LEVELS, SELL_LEVELS = vol_buy, vol_sell
                    vol_stop_loss_raw = BUY_LEVELS[0] * (1 - STOP_LOSS_BUFFER_PCT / 100)
                    if DYNAMIC_STOP_LOSS_ATR_MULT > 0:
                        dyn_sl = BUY_LEVELS[0] - atr * DYNAMIC_STOP_LOSS_ATR_MULT
                        STOP_LOSS = round(min(vol_stop_loss_raw, dyn_sl), 2)
                        log(f"[VolGrid] STOP_LOSS=${STOP_LOSS:.2f} "
                            f"(buffer=${vol_stop_loss_raw:.2f}, dyn=${dyn_sl:.2f})")
                    else:
                        STOP_LOSS = round(vol_stop_loss_raw, 2)
                    log(f"[VolGrid] Recomputed BUY={BUY_LEVELS} SELL={SELL_LEVELS} STOP_LOSS=${STOP_LOSS:.2f}")
                    grid_recomputed = True
                else:
                    log(f"[VolGrid] Volatility grid failed, using fixed range {FALLBACK_RANGE_PCT}%")

            elif ENABLE_GRID_DRIFT and state.get("grid_center") is not None:
                # [VolGrid] Grid drift: center only shifts DOWN when price falls (never up)
                drift_thresh = state["grid_center"] * (1 - GRID_DRIFT_THRESHOLD_PCT / 100)
                if price < drift_thresh:
                    shift_pct = min(GRID_DRIFT_MAX_SHIFT_PCT,
                                    (state["grid_center"] - price) / state["grid_center"] * 100)
                    new_center = state["grid_center"] * (1 - shift_pct / 100)
                    log(f"[VolGrid] GRID DRIFT: center {state['grid_center']:.2f} → {new_center:.2f} "
                        f"(price ${price:.2f} < ${drift_thresh:.2f})")
                    effective_grid_center = new_center
                    grid_center = new_center
                    state["grid_center"] = new_center
                    # [Fix#8] Recompute BUY/SELL levels after drift shift
                    BUY_LEVELS, SELL_LEVELS = compute_grid_levels(
                        effective_grid_center, GRID_RANGE_PCT, GRID_TIERS)
                    STOP_LOSS = round(BUY_LEVELS[0] * (1 - STOP_LOSS_BUFFER_PCT / 100), 2)
                    log(f"[VolGrid] DRIFT recomputed BUY={BUY_LEVELS} SELL={SELL_LEVELS} STOP_LOSS=${STOP_LOSS:.2f}")
                    grid_recomputed = True

            if not grid_recomputed:
                # [VolGrid] Periodic re-center: if price has drifted far from init_grid_center, refresh
                if abs(price - init_grid_center) / init_grid_center > 0.05:
                    log(f"[VolGrid] Price ${price:.2f} drifted >5% from init center ${init_grid_center:.2f}, "
                        f"refreshing static grid")
                    BUY_LEVELS, SELL_LEVELS = compute_grid_levels(
                        init_grid_center, GRID_RANGE_PCT, GRID_TIERS)
                    STOP_LOSS = round(BUY_LEVELS[0] * (1 - STOP_LOSS_BUFFER_PCT / 100), 2)
                    grid_center = init_grid_center

            # [Fix#1+Fix#2] ===== STOP LOSS — highest priority, fires regardless of pending grid txs =====
            # [Fix#2] Re-arm stop-loss after cooldown ends (5 min cooldown after first fire)
            if state.get("stop_loss_triggered"):
                if now >= state.get("stop_loss_cooldown_until", 0):
                    log(f"  [StopLoss] Cooldown ended, re-arming — grids cleared, resuming at market")
                    state["stop_loss_triggered"] = False
                    state["trail_armed"] = False
                    state["trail_peak"] = None
                    state["buy_triggered"] = {}
                    state["sell_triggered"] = {}
                    # [Fix#6] Also clear trail_triggered flag
                    state["trail_triggered"] = False
                    save_state(state)
                    time.sleep(CHECK_INTERVAL)
                    continue  # [Fix#7] Prevent double-liquidation in same loop
                else:
                    remaining = state["stop_loss_cooldown_until"] - now
                    log(f"  [StopLoss] In cooldown, {remaining:.0f}s remaining")

            if price <= STOP_LOSS:
                if should_alert(state, "STOP_LOSS", f"STOP LOSS TRIGGERED! ETH=${price:.2f}"):
                    # [Fix#1] Separate pending_stop_loss flag so stop-loss fires even when grid tx pending
                    state["pending_stop_loss"] = True
                    save_state(state)
                    ok = liquidate_all(price, state=state)
                    if ok:
                        telegram_notify(f"🛑 STOP LOSS triggered!\nPrice: ${price:.2f}\nAll grids reset.", "CRITICAL")
                        state["pending_stop_loss"] = False
                        state["trail_armed"] = False
                        state["trail_peak"] = None
                        state["buy_triggered"] = {}
                        state["sell_triggered"] = {}
                        # [Fix#2] Start 5-minute cooldown after successful stop-loss
                        state["stop_loss_triggered"] = True
                        state["stop_loss_cooldown_until"] = now + 300
                        log(f"  [StopLoss] Fired and cooling down for 300s (until {time.strftime('%H:%M:%S', time.localtime(state['stop_loss_cooldown_until']))})")
                    else:
                        state["pending_stop_loss"] = True
                        log("  Stop loss failed, retry next cycle")
                save_state(state)
                time.sleep(CHECK_INTERVAL)
                continue

            # ===== [Fix#1] Pending grid-tx pre-check (does NOT block stop-loss) =====
            # Stop-loss above uses separate pending_stop_loss flag, so grid trades
            # can wait here without blocking emergency liquidations.
            if state.get("pending_tx") and state.get("pending_tx_sent"):
                elapsed = now - state["pending_tx_sent"]
                if elapsed > PENDING_TX_TIMEOUT:
                    # [Fix#3] TX timeout — query receipt to determine next action
                    log(f"⚠️  Pending TX {state['pending_tx'][:20]}... timed out after {elapsed:.0f}s")
                    receipt_result = rpc("eth_getTransactionReceipt", [state["pending_tx"]])
                    receipt = receipt_result.get("result")
                    if receipt:
                        if receipt.get("status") == "0x1":
                            log(f"  TX succeeded on-chain (gas used: {receipt.get('gasUsed','?')})")
                        else:
                            log(f"  TX FAILED on-chain — clearing pending state")
                            # [Fix#10] Rollback nonce for failed tx
                            nonce_mgr.rollback()
                    else:
                        log(f"  TX not found on-chain — clearing pending state")
                        # [Fix#10] Rollback nonce for unknown tx
                        nonce_mgr.rollback()
                    # Always clear pending_tx to allow next trade
                    state["pending_tx"] = None
                    state["pending_tx_sent"] = 0.0
                elif elapsed > 120:
                    log(f"⚠️  Previous TX {state['pending_tx'][:20]}... pending for {elapsed:.0f}s — waiting for confirmation")
                else:
                    log(f"  Waiting for previous TX {state['pending_tx'][:20]}... ({elapsed:.0f}s elapsed)")
                save_state(state)
                time.sleep(CHECK_INTERVAL)
                continue

            # ===== TRAILING STOP =====
            # [Fix#6] Cooldown: clear trail_triggered after cooldown expires
            if state.get("trail_cooldown_until", 0) > 0 and now >= state["trail_cooldown_until"]:
                if state.get("trail_triggered"):
                    log(f"[TRAIL] Cooldown expired, resetting trail state")
                state["trail_triggered"] = False
                state["trail_cooldown_until"] = 0.0

            # [Fix#1+Fix#8] Skip if a liquidation is already pending or stop-loss is active
            if state.get("pending_liquidation") or state.get("pending_stop_loss") or state.get("stop_loss_triggered") or state.get("trail_triggered"):  # [Fix#6] added trail_triggered
                log(f"  Liquidation already pending, waiting for confirmation...")
                save_state(state)
                time.sleep(CHECK_INTERVAL)
                continue
            # Activate trail when price crosses above TRAIL_TRIGGER_PRICE
            if not state["trail_armed"] and price >= TRAIL_TRIGGER_PRICE:
                state["trail_armed"] = True
                state["trail_peak"] = price
                log(f"📈 TRAIL ARMED! Peak: ${price:.2f}, trigger: ${price*(1-TRAIL_PCT):.2f}")

            # Update peak (only up)
            if state["trail_armed"] and state["trail_peak"] is not None:
                new_peak = price
                old_trigger = state["trail_peak"] * (1 - TRAIL_PCT)
                if new_peak > state["trail_peak"]:
                    state["trail_peak"] = new_peak
                    new_trigger = new_peak * (1 - TRAIL_PCT)
                    log(f"📈 Trail peak updated: ${new_peak:.2f}, trigger: ${new_trigger:.2f}")
                    old_trigger = new_trigger  # update for next check

            # Trail triggered: price fell TRAIL_PCT from peak
            if state["trail_armed"] and state["trail_peak"] is not None:
                trail_trigger = state["trail_peak"] * (1 - TRAIL_PCT)
                if price <= trail_trigger:
                    log(f"🎯 TRAIL STOP! Price ${price:.2f} <= trigger ${trail_trigger:.2f} (peak ${state['trail_peak']:.2f})")
                    if should_alert(state, "TRAIL_TRIGGERED", f"TRAIL STOP triggered! ETH=${price:.2f}"):
                        trail_peak = state["trail_peak"]
                        ok = liquidate_all(price, state=state)
                        if ok:
                            telegram_notify(f"📉 TRAILING STOP triggered!\nPeak: ${trail_peak:.2f}\nProfit protected.", "WARNING")
                            state["trail_armed"] = False
                            state["trail_peak"] = None
                            state["trail_triggered"] = True  # [Fix#6] Mark trail as triggered
                            state["trail_cooldown_until"] = now + 300  # [Fix#6] 5-minute cooldown before re-arm
                            state["buy_triggered"] = {}
                            state["sell_triggered"] = {}
                            log("  Trail exit complete, grids reset")
                        else:
                            # [Fix#9] Clear stale pending_tx so next cycle doesn't wait 180s for timeout
                            if state.get("pending_tx"):
                                log("  Trail stop failed, clearing pending_tx")
                                state["pending_tx"] = None
                                state["pending_tx_sent"] = 0.0   # [Fix] Use 0.0 for consistency with timeout clear path (L960)
                            state["trail_triggered"] = True
                            state["trail_cooldown_until"] = now + 300
                save_state(state)
                time.sleep(CHECK_INTERVAL)
                continue

            # ===== GRID SELL (only when trail not active) =====
            for level in SELL_LEVELS:
                # [VolGrid] RSI overbought filter
                if skip_sell:
                    break  # skip all sell levels when overbought
                if price >= level and not state["sell_triggered"].get(level):
                    # [Enhancement C] Safety: ensure enough ETH to pay for gas
                    eth_bal_now = get_eth()
                    weth_bal_now = get_weth()
                    if eth_bal_now + weth_bal_now < MIN_BALANCE_ETH:
                        log(f"  ETH balance {eth_bal_now + weth_bal_now:.6f} < MIN_BALANCE_ETH {MIN_BALANCE_ETH:.6f}, skipping sell level ${level}")
                        continue
                    if eth_bal_now + weth_bal_now < SWAP_AMOUNT_WETH + GAS_RESERVE_ETH:
                        log(f"  Insufficient balance for sell level ${level}")
                        continue
                    if now - state["last_trade"] < TRADE_COOLDOWN:
                        continue
                    log(f"📤 GRID SELL Level ${level} (price=${price:.2f})")
                    # Wrap ETH first
                    if eth_bal_now > SWAP_AMOUNT_WETH + GAS_RESERVE_ETH:
                        ok = wrap_eth(int(SWAP_AMOUNT_WETH * 1e18), state=state)
                        if not ok:
                            log(f"  Wrap failed, skip sell level ${level}")
                            continue
                        time.sleep(5)
                    ok = swap_weth_for_usdc(int(SWAP_AMOUNT_WETH * 1e18), price, state=state)
                    if ok:
                        state["sell_triggered"][level] = now
                        state["last_trade"] = now
                        # [Fix#11] Track consecutive losses
                        state["consecutive_loss"] = state.get("consecutive_loss", 0) + 1
                        if state["consecutive_loss"] >= CONSECUTIVE_LOSS_PAUSE:
                            telegram_notify(f"⚠️ {state['consecutive_loss']} consecutive losses.\nBot paused 1 hour.", "WARNING")
                            log(f"  ⚠️ Paused 1h due to {state['consecutive_loss']} consecutive losses")
                            save_state(state)
                            time.sleep(3600)
                        log(f"  Sell level ${level} filled!")
                    time.sleep(5)
                    break  # one level per cycle

            # ===== GRID BUY (only when trail not active) =====
            # [Bug#1] Fix: cooldown check before swap; single swap; log uses actual level; usdc_needed calc unified
            for level in BUY_LEVELS:
                # [VolGrid] RSI oversold filter
                if skip_buy:
                    break  # skip all buy levels when oversold
                if price <= level and not state["buy_triggered"].get(level):
                    # [Bug#1.4] usdc_needed computed once here; used consistently below
                    usdc_needed = int(level * SWAP_AMOUNT_WETH * 1.1 * 1e6)
                    if usdc_bal * 1e6 < usdc_needed:
                        log(f"  Insufficient USDC for buy level ${level} (have ${usdc_bal:.6f}, need ${usdc_needed/1e6:.2f})")
                        continue
                    # [Bug#1.1] Cooldown check BEFORE swap
                    if now - state["last_trade"] < TRADE_COOLDOWN:
                        continue
                    # [Bug#1.2] Single swap only; [Bug#1.3] log uses actual level, not hardcoded $1760
                    # [Issue#3] Pass level price as slippage reference
                    log(f"💰 GRID BUY Level ${level} (price=${price:.2f})")
                    ok = swap_usdc_for_weth(usdc_needed, price, slippage_price=level, state=state)
                    if ok:
                        state["buy_triggered"][level] = now
                        state["last_trade"] = now
                        # [Fix#11] Reset consecutive loss counter on successful buy
                        state["consecutive_loss"] = 0
                        log(f"  Buy level ${level} filled!")
                    time.sleep(5)
                    break  # one level per cycle

            state["last_price"] = price
            save_state(state)

        # [Fix#12] Graded exception handling — don't swallow everything
        except (requests.exceptions.RequestException, ConnectionError) as e:  # [Fix#6] added `as e`
            log(f"Network error: {e}, retrying...")
            telegram_notify(f"⚠️ Network error persists.\nLast error: {e}", "WARNING")
            time.sleep(CHECK_INTERVAL * 2)
            continue
        except ValueError as e:
            log(f"FATAL: Data validation error — {e}")
            telegram_notify(f"🚨 FATAL ERROR — Bot exiting.\nError: {e}", "CRITICAL")
            should_alert(state, "FATAL_ERROR", f"Data error: {e}")
            sys.exit(1)
        except Exception as e:
            import traceback
            log(f"Loop error: {traceback.format_exc()[:500]}")
            save_state(state)
            time.sleep(CHECK_INTERVAL)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
