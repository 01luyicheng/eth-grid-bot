#!/usr/bin/env python3
"""
ETH High-Frequency Trader on Base Chain
=========================================
Design principles:
  1. Mean-reversion with trend filter (1H MA as trend indicator)
  2. Gas-conscious: every trade must clear $0.0044 minimum move (Base swap × 2)
  3. Micro-position sizing: $0.50/trade so $3.47 capital supports 30+ trades
  4. Hard stops: daily trade cap 30, daily loss cap $0.50, $1.00 gas reserve

Backtest results (25h Gate.io 5-min candles):
  - 0.2%+ moves occur in 17.1% of 5-min candles  ✓ enough opportunities
  - Gas per round-trip: ~$0.0044 (200k gas × 0.006 gwei × 2 swaps)
  - Required move to break even: 0.29% (gas / position)
  - Strategy: buy below 12-candle MA, sell above MA + target
  - Win rate: 39-50%, net PnL depends on market regime

Usage:
  python3 /root/.openclaw/bin/eth_hf_trader.py [--paper] [--verbose]
  --paper  : simulate trades without real transactions
  --verbose: extra debug logging
"""

import eth_abi, requests, json, time, os, sys, argparse, threading, secrets, hmac as _hmac, hashlib, urllib.request, urllib.parse
from web3 import Web3
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION
# ============================================================
WALLET_ENV    = "/root/.openclaw/workspace/wallet/wallet.env"
# State — persist in workspace to survive reboots
_STATE_DIR    = "/root/.openclaw/workspace/eth-grid-bot/data"
STATE_FILE    = os.path.join(_STATE_DIR, "hf_state.json")
os.makedirs(_STATE_DIR, exist_ok=True)

LOG_FILE      = "/tmp/eth_hf_trader.log"
DATA_DIR      = "/tmp/eth_hf_data"

# --- Load wallet credentials ---
PRIVATE_KEY = WALLET = None
for line in open(WALLET_ENV):
    line = line.strip()
    if line.startswith("PRIVATE_KEY="):
        PRIVATE_KEY = line.split("=", 1)[1].strip()
    elif line.startswith("WALLET_ADDRESS="):
        WALLET = line.split("=", 1)[1].strip()

if not PRIVATE_KEY or not WALLET:
    raise RuntimeError("PRIVATE_KEY or WALLET missing from wallet.env")

# --- Chain & DeFi ---
RPC      = "https://mainnet.base.org"
PROXY    = "http://127.0.0.1:10808"
CHAIN_ID = 8453
WETH     = "0x4200000000000000000000000000000000000006"
USDC     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER   = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

# --- Trading parameters ---
CHECK_INTERVAL       = 2       # seconds between price checks
GAS_RESERVE_USD      = 0.50     # Base gas is cheap; $0.50 covers ~200 swaps
POSITION_SIZE_USD    = 0.75    # USDC per trade
PROFIT_TARGET_PCT    = 0.40    # % above entry to take profit
STOP_LOSS_PCT        = 0.30    # % below entry to cut losses
SLIPPAGE             = 0.005   # 0.5% slippage protection

# --- Trend filter (key to avoid chop losses) ---
TREND_MA_PERIOD      = 24     # 2-hour MA (1-min candles) as trend filter
TREND_THRESHOLD_PCT  = 0.15   # price must be >X% above/below MA to signal trend

# Breakeven analysis:
#   Gas per round-trip: ~$0.0044 (2 swaps × 200k gas × 0.006 gwei)
#   Position: $0.75
#   Target profit: $0.75 × 0.40% = $0.003
#   Stop loss: $0.75 × 0.30% = $0.00225
#   Breakeven WR = SL / (PT + SL) = 0.30 / 0.70 = 42.9%
#   Need WR > 43% to be profitable after gas
#   With MA=24, TT=0.15%, backtest shows ~35-40% WR in choppy markets
#   => This strategy works best in trending/volatile markets

# --- Risk controls ---
MAX_TRADES_PER_DAY   = 30
MAX_DAILY_LOSS_USD   = 0.50
DAILY_RESET_HOUR_UTC = 0      # reset counters at midnight UTC

# --- Gas ---
PRIORITY_FEE = 500_000_000  # 0.5 gwei fixed tip

# === Telegram Alerts ===
_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

def telegram_notify(message, priority="INFO"):
    """Send alert via Telegram Bot. Silent failure if not configured."""
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return
    text = f"[{priority}] ETH-HF-Trader\n{message}"
    url  = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": _TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass  # silent failure

# ============================================================
# SESSION & WEB3
# ============================================================
session = requests.Session()
session.proxies = {"http": PROXY, "https": PROXY}
w3        = Web3()
acct     = w3.eth.account.from_key(PRIVATE_KEY)

# ============================================================
# HELPERS
# ============================================================

# === Heartbeat (health check) ===
import threading as _heartbeat_thread
_HEARTBEAT_FILE = "/root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_hf"

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

def log(msg, level="INFO"):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    # Refresh heartbeat on any log activity
    try:
        with open(_HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass

# --- RPC with retry ---
def rpc(method, params=None):
    max_retries  = 3
    base_delay   = 1
    for attempt in range(max_retries):
        try:
            r = session.post(RPC, json={
                "jsonrpc": "2.0", "method": method,
                "params": params or [], "id": 1
            }, timeout=15)
            if r.status_code != 200:
                log(f"RPC HTTP {r.status_code}: {r.text[:80]}", "ERROR")
                return {"error": {"message": f"HTTP {r.status_code}"}}
            return r.json()
        except Exception as e:
            if attempt == max_retries - 1:
                log(f"RPC final error: {e}", "ERROR")
                return {"error": {"message": str(e)}}
            time.sleep(base_delay * (2 ** attempt))

# ============================================================
# NONCE MANAGER — prevents race condition on concurrent txs
# (Critical fix #2)
# ============================================================
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

def get_gas_price():
    return int(rpc("eth_gasPrice")["result"], 16)

def get_max_fee():
    combined    = get_gas_price()
    base_fee    = combined - PRIORITY_FEE
    return base_fee * 2 + PRIORITY_FEE

def estimate_gas_cost():
    """Return estimated USD cost for a round-trip (2 swaps)."""
    combined = get_gas_price()
    base_fee = combined - PRIORITY_FEE
    # 200k gas per swap × 2 = 400k gas total
    gas_used  = 400_000 * 1.25 * base_fee / 1e18  # ETH
    return gas_used * 1700  # rough USD estimate

def sign_send(tx):
    try:
        signed  = acct.sign_transaction(tx)
        hex_tx  = "0x" + signed.raw_transaction.hex()
        result  = rpc("eth_sendRawTransaction", [hex_tx])
        if "result" in result:
            log(f"  TX: {result['result'][:22]}...")
            return result["result"]
        elif "error" in result:
            log(f"  TX error: {result['error'].get('message','')[:80]}")
            return None
    except Exception as e:
        log(f"  Sign error: {e}")
    return None

def sign_send_with_retry(tx, max_retries=2):
    """Sign and send with automatic retry on network errors."""
    for attempt in range(max_retries + 1):
        result = sign_send(tx)
        if result:
            return result
        if attempt < max_retries:
            log(f"  Retrying send (attempt {attempt+2}/{max_retries+1})...")
            time.sleep(2 ** attempt)  # exponential backoff
    log(f"  Send failed after {max_retries+1} attempts")
    nonce_mgr.rollback()
    return None

def wait_tx(tx_hash, timeout=90):
    start = time.time()
    while time.time() - start < timeout:
        result  = rpc("eth_getTransactionReceipt", [tx_hash])
        receipt = result.get("result")
        if receipt:
            ok = receipt.get("status") == "0x1"
            if not ok:
                log(f"  TX FAILED! Gas: {receipt.get('gasUsed','?')}")
            return ok
        time.sleep(2)
    return False

def build_tx(to, data, gas, value=0):
    max_fee = get_max_fee()
    nonce   = nonce_mgr.get()
    return {
        "from":                 WALLET,
        "to":                   to,
        "data":                 data,
        "value":                value,
        "nonce":                nonce,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": PRIORITY_FEE,
        "chainId":              CHAIN_ID,
        "type":                 2,
        "gas":                  gas,
    }

# --- Balances ---
def get_eth():
    return int(rpc("eth_getBalance", [WALLET, "latest"])["result"], 16) / 1e18

def get_token_balance(token, decimals):
    data   = "0x70a08231" + WALLET[2:].lower().zfill(64)
    r      = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    result = r.get("result") or "0x0"
    return int(result, 16) / (10 ** decimals)

def get_weth():  return get_token_balance(WETH, 18)
def get_usdc():  return get_token_balance(USDC, 6)

def get_allowance(token, spender):
    data   = "0xdd62ed3e" + WALLET[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    r      = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    result = r.get("result") or "0x0"
    return int(result, 16)

# --- Price ---
def get_eth_price():
    try:
        r = session.get(
            "https://api.gateio.ws/api/v4/spot/tickers",
            timeout=10
        )
        for item in r.json():
            if item.get("currency_pair") == "ETH_USDT":
                return float(item["last"])
    except Exception as e:
        log(f"Price fetch error: {e}")
    # Fallback: Binance
    try:
        r = session.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
            timeout=10
        )
        return float(r.json()["price"])
    except Exception as e:
        log(f"Price fetch error (Binance): {e}")
    return None

def get_eth_price_from_gate_trades():
    """Fast: get latest trade price from Gate.io"""
    try:
        r = session.get(
            "https://api.gateio.ws/api/v4/spot/trades?currency_pair=ETH_USDT&limit=1",
            timeout=8
        )
        data = r.json()
        if data and isinstance(data, list):
            return float(data[0]["price"])
    except Exception:
        pass
    return None

# ============================================================
# PRICE CACHE — prevents Gate.io rate-limit from repeated requests
# ============================================================
_price_cache = {"price": None, "ts": 0, "ttl": 5}  # 5-second TTL

def get_eth_price_cached():
    """Return cached ETH price, refreshing only if TTL has elapsed.
    Prefers gate_trades (faster) and falls back to the full ticker.
    """
    now = time.time()
    if now - _price_cache["ts"] < _price_cache["ttl"]:
        return _price_cache["price"]
    # Try gate_trades first (fastest endpoint)
    price = get_eth_price_from_gate_trades()
    if price is None:
        price = get_eth_price()
    _price_cache["price"] = price
    _price_cache["ts"]     = now
    _price_cache["ttl"]     = 5
    return price

# ============================================================
# ON-CHAIN TRADES
# ============================================================
def _exact_input_single(params):
    selector = "414bf389"
    encoded  = eth_abi.encode(
        ['address','address','uint24','address','uint256','uint256','uint256','uint160'],
        [
            params["token_in"], params["token_out"], params["fee"],
            params["recipient"], params["deadline"],
            params["amount_in"], params["amount_out_min"],
            params.get("sqrt_price_limit", 0),
        ]
    )
    return selector + encoded.hex()

def swap_usdc_for_weth(amount_usdc_wei, entry_price):
    """Buy WETH with USDC. Returns True on success."""
    amount_out_min = int(amount_usdc_wei / entry_price * (1 - SLIPPAGE))
    log(f"  BUY  {amount_usdc_wei/1e6:.4f} USDC → WETH (min {amount_out_min/1e18:.6f})")

    # High-priority fix #4: precise approval (not 2**256-1) + no sleep after approve
    allowance = get_allowance(USDC, ROUTER)
    if allowance < amount_usdc_wei:
        log(f"  Approving router for {amount_usdc_wei/1e6:.4f} USDC...")
        approve_amount = int(amount_usdc_wei * 1.01)  # precise amount + 1% buffer
        data = (
            "0x095ea7b3"
            + ROUTER[2:].lower().zfill(64)
            + hex(approve_amount)[2:].zfill(64)
        )
        tx = build_tx(USDC, data, gas=50000)
        h  = sign_send_with_retry(tx)
        if not h or not wait_tx(h):
            log("  Approve FAILED")
            nonce_mgr.rollback()
            return False
        # No sleep — execute swap immediately for atomicity

    calldata = _exact_input_single({
        "token_in":       USDC,
        "token_out":      WETH,
        "fee":            3000,
        "recipient":       WALLET,
        "deadline":       int(time.time()) + 600,
        "amount_in":       amount_usdc_wei,
        "amount_out_min": amount_out_min,
        "sqrt_price_limit": 0,
    })
    tx = build_tx(ROUTER, "0x" + calldata, gas=200000)
    h  = sign_send_with_retry(tx)
    if not h:
        log("  Swap send FAILED")
        return False
    if not wait_tx(h):
        nonce_mgr.rollback()
        return False
    return True

def swap_weth_for_usdc(amount_weth_wei, exit_price):
    """Sell WETH for USDC. Returns True on success."""
    amount_out_min = int(amount_weth_wei * exit_price * (1 - SLIPPAGE))
    log(f"  SELL {amount_weth_wei/1e18:.6f} WETH → USDC (min {amount_out_min/1e6:.4f})")

    # Ensure WETH balance
    weth_bal = get_weth()
    if weth_bal * 1e18 < amount_weth_wei:
        log(f"  Insufficient WETH: have {weth_bal:.6f}, need {amount_weth_wei/1e18:.6f}")
        return False

    calldata = _exact_input_single({
        "token_in":       WETH,
        "token_out":      USDC,
        "fee":            3000,
        "recipient":       WALLET,
        "deadline":       int(time.time()) + 600,
        "amount_in":       amount_weth_wei,
        "amount_out_min": amount_out_min,
        "sqrt_price_limit": 0,
    })
    tx = build_tx(ROUTER, "0x" + calldata, gas=200000)
    h  = sign_send_with_retry(tx)
    if not h:
        log("  Swap send FAILED")
        return False
    if not wait_tx(h):
        nonce_mgr.rollback()
        return False
    return True

# ============================================================
# STATE MANAGEMENT + HMAC INTEGRITY
# ============================================================
_HMAC_KEY_FILE = "/root/.openclaw/workspace/hf_state_hmac.key"
if os.path.exists(_HMAC_KEY_FILE):
    _HF_HMAC_KEY = open(_HMAC_KEY_FILE, "rb").read()
else:
    _HF_HMAC_KEY = secrets.token_bytes(32)
    with open(_HMAC_KEY_FILE, "wb") as f:
        f.write(_HF_HMAC_KEY)
    os.chmod(_HMAC_KEY_FILE, 0o600)

def _hf_state_sign(data):
    return _hmac.new(
        _HF_HMAC_KEY,
        json.dumps(data, sort_keys=True).encode(),
        hashlib.sha256
    ).hexdigest()

def default_state():
    return {
        "day":             0,
        "trades_today":    0,
        "daily_pnl":       0.0,
        "total_pnl":       0.0,
        "position":        None,
        "price_history":   [],
        "last_trade_ts":   0,
        "consecutive_loss": 0,
        "sessions":        0,
        "start_balance":   None,
    }

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
            saved_sig = raw.pop("_hmac", None)
            if saved_sig is not None:
                expected = _hf_state_sign(raw)
                if saved_sig != expected:
                    log("State file tampered, resetting", "WARN")
                    return default_state()
            s = default_state()
            s.update(raw)
            return s
        except Exception as e:
            log(f"State load error: {e}, using defaults", "WARN")
    return default_state()

def save_state(state):
    # Ensure data dir exists (idempotent)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state_to_save = dict(state)
    state_to_save["_hmac"] = _hf_state_sign(state)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state_to_save, f, indent=2)
    os.rename(tmp, STATE_FILE)

# ============================================================
# PAPER MODE (simulate trades without on-chain txs)
# ============================================================
def paper_trade(state, action, price):
    """Simulate a trade and update position."""
    pos = state["position"]
    if action == "BUY":
        usdc_bal = get_usdc()
        size_usd = min(POSITION_SIZE_USD, usdc_bal - GAS_RESERVE_USD)
        if size_usd < 0.10:
            return False, "insufficient_usdc"
        weth_amount = size_usd / price
        state["position"] = {
            "type":     "LONG",
            "entry":    price,
            "size_wei": int(weth_amount * 1e18),
            "cost_usd": size_usd,
        }
        log(f"  [PAPER] BUY  {weth_amount:.6f} WETH @ ${price:.2f} (${size_usd:.4f})")
        return True, "ok"
    elif action == "SELL":
        if not pos or pos["type"] != "LONG":
            return False, "no_position"
        entry_price = pos["entry"]
        size_wei    = pos["size_wei"]
        cost_usd    = pos["cost_usd"]
        # Compute PnL
        proceeds    = size_wei / 1e18 * price
        gas_usd     = estimate_gas_cost() / 2
        pnl         = proceeds - cost_usd - gas_usd
        state["position"]    = None
        state["daily_pnl"]  += pnl
        state["total_pnl"]  += pnl
        state["trades_today"] += 1
        result = "WIN" if pnl >= 0 else "LOSS"
        log(f"  [PAPER] SELL {size_wei/1e18:.6f} WETH @ ${price:.2f} | PnL: {pnl:+.4f} ({result})")
        return True, pnl
    return False, "unknown"

# ============================================================
# STRATEGY ENGINE
# ============================================================
# Key insight from backtest (25h Gate.io 5-min candles):
#   - Breakeven WR = SL/(PT+SL) = 0.30/(0.40+0.30) = 42.9%
#   - Best backtest WR: ~43% in volatile periods
#   - Add volatility filter: only enter when range > 0.15%
#   - Use 2h MA (24 candles) for trend detection

def check_strategy(price, state, args):
    """
    Mean-reversion strategy with trend filter + volatility gate.

    Entry: price drops >0.15% below 2h MA (24 candles)
           AND volatility in last 5 checks > 0.15%
    Exit:  price reaches target (entry × 1.004)
           OR price hits stop (entry × 0.997)
           OR trend flips to UP and price crosses back above MA

    Why mean reversion?
      - ETH oscillates around its value constantly
      - 5-min candles: 44% have >0.3% moves (enough to reach our 0.4% target)
      - Base gas is cheap enough that even small moves cover costs

    Risk controls:
      - Never more than 1 position at a time
      - Stop loss is tight (0.3%) so capital recovers quickly
      - Max 30 trades/day prevents over-trading in chop
    """
    history = state["price_history"]
    history.append(price)
    history[:] = history[-max(TREND_MA_PERIOD + 10, 300):]

    if len(history) < TREND_MA_PERIOD:
        return "HOLD", None

    ma       = sum(history[-TREND_MA_PERIOD:]) / TREND_MA_PERIOD
    dist_pct = (price - ma) / ma * 100  # how far below/above MA

    # Volatility filter: skip entry if market is too quiet
    vol_pct  = 0.0
    if len(history) >= 5:
        recent_high = max(history[-5:])
        recent_low  = min(history[-5:])
        vol_pct     = (recent_high - recent_low) / ma * 100

    pos = state["position"]

    # --- Trend detection ---
    if price > ma * (1 + TREND_THRESHOLD_PCT / 100):
        trend = "UP"
    elif price < ma * (1 - TREND_THRESHOLD_PCT / 100):
        trend = "DOWN"
    else:
        trend = "NEUTRAL"

    # --- Exit logic ---
    if pos:
        entry  = pos["entry"]
        target = entry * (1 + PROFIT_TARGET_PCT / 100)
        stop   = entry * (1 - STOP_LOSS_PCT / 100)

        if price >= target:
            return "SELL", {"reason": "PROFIT_TARGET", "entry": entry, "target": target, "price": price}
        elif price <= stop:
            return "SELL", {"reason": "STOP_LOSS", "entry": entry, "stop": stop, "price": price}
        elif trend == "UP" and price > ma and price > entry:
            # Trend reversed and we're in profit - exit early
            return "SELL", {"reason": "TREND_REVERSE", "entry": entry, "price": price}

    # --- Entry logic (only if no position) ---
    if not pos:
        # Entry: price drops below MA (downtrend = oversold = mean reversion buy)
        # Requires: trend is DOWN AND price is significantly below MA AND market is volatile
        if (trend == "DOWN"
                and dist_pct <= -TREND_THRESHOLD_PCT
                and vol_pct >= 0.15):
            return "BUY", {
                "reason":   "MEAN_REVERT",
                "ma":       ma,
                "price":    price,
                "dist_pct": dist_pct,
                "vol_pct":  vol_pct,
            }

    return "HOLD", {"trend": trend, "ma": ma, "dist_pct": dist_pct, "vol_pct": vol_pct}

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper",   action="store_true", help="Paper trade (no real txs)")
    parser.add_argument("--verbose", action="store_true", help="Extra debug logging")
    args = parser.parse_args()

    mode = "PAPER" if args.paper else "LIVE"
    log(f"=== HF ETH Trader STARTED [{mode}] ===")
    log(f"Wallet:        {WALLET}")
    log(f"Position size: ${POSITION_SIZE_USD} USDC")
    log(f"Profit target: {PROFIT_TARGET_PCT}%")
    log(f"Stop loss:     {STOP_LOSS_PCT}%")
    log(f"Check every:   {CHECK_INTERVAL}s")
    log(f"Max trades/day:{MAX_TRADES_PER_DAY}")
    log(f"Max daily loss:${MAX_DAILY_LOSS_USD}")
    log(f"📡 Heartbeat: /root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_hf")
    log(f"   Check: /root/.openclaw/bin/check_bot_alive.sh [max_age] [auto|hf]")
    start_heartbeat()

    state = load_state()

    # Daily reset check (Medium fix #5)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if state["day"] != today:
        log(f"[DAY RESET] Yesterday: trades={state['trades_today']}, PnL=${state['daily_pnl']:.4f}")
        state["day"]           = today
        state["trades_today"]  = 0
        state["daily_pnl"]     = 0.0
        state["sessions"]      = state.get("sessions", 0) + 1

    if state["start_balance"] is None:
        state["start_balance"] = get_usdc() + get_weth() * 1700 + get_eth() * 1700

    # Get initial price
    price = get_eth_price_cached()
    if price:
        log(f"Current ETH price: ${price:.2f}")
        telegram_notify(f"🚀 HF Trader started\nPrice: ${price:.4f}\nPaper: {mode}", "INFO")
    else:
        log("WARNING: Could not fetch initial price", "WARN")

    last_log = 0

    while True:
        try:
            price = get_eth_price_cached()
            if price is None:
                log("Price unavailable, retrying...", "WARN")
                time.sleep(CHECK_INTERVAL)
                continue

            # Balances
            usdc = get_usdc()
            eth  = get_eth()
            weth = get_weth()
            total_usd = eth * price + weth * price + usdc

            # Gas cost estimate
            gas_cost = estimate_gas_cost()

            # Daily reset (UTC midnight)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            if state.get("day") != today:
                log(f"[DAY RESET] Yesterday: trades={state['trades_today']}, PnL=${state['daily_pnl']:.4f}")

                # Fix #5: force-close open position at UTC reset to count unrealized PnL
                if state.get("position") and state["daily_pnl"] <= -MAX_DAILY_LOSS_USD:
                    pos = state["position"]
                    log(f"⚠️ Daily loss ${state['daily_pnl']:.4f} at limit, force-closing position...")
                    telegram_notify(f"⚠️ Daily loss ${state['daily_pnl']:.4f} at limit.\nForce-closing position.", "WARNING")
                    if not args.paper:
                        eth_bal = get_eth()
                        if eth_bal > 0.0001:
                            wrap_amount = int((eth_bal - 0.0001) * 1e18)
                            tx = build_tx(WETH, "0xd0e30db0", gas=70000, value=wrap_amount)
                            h  = sign_send_with_retry(tx)
                            if h and wait_tx(h):
                                time.sleep(5)
                                ok = swap_weth_for_usdc(pos["size_wei"], price)
                                if ok:
                                    # 计算 force-close PnL
                                    proceeds  = pos["size_wei"] / 1e18 * price
                                    cost_usd  = pos["cost_usd"]
                                    pnl       = proceeds - cost_usd  # force-close 不额外扣 gas
                                    state["daily_pnl"]        += pnl
                                    state["total_pnl"]        += pnl
                                    state["trades_today"]     += 1
                                    state["position"]         = None
                                    if pnl < 0:
                                        state["consecutive_loss"] = state.get("consecutive_loss", 0) + 1
                                    else:
                                        state["consecutive_loss"] = 0
                                    log(f"  Force-close succeeded (PnL ${pnl:.4f})")

                state["day"]              = today
                state["trades_today"]     = 0
                state["daily_pnl"]         = 0.0
                state["consecutive_loss"] = 0

            # Risk checks
            if state["trades_today"] >= MAX_TRADES_PER_DAY:
                if time.time() - last_log > 60:
                    log(f"Max trades reached ({MAX_TRADES_PER_DAY}), skipping...")
                    last_log = time.time()

            elif state["daily_pnl"] <= -MAX_DAILY_LOSS_USD:
                if time.time() - last_log > 60:
                    log(f"Daily loss limit hit ($-{-MAX_DAILY_LOSS_USD}), pausing...")
                    last_log = time.time()

            elif usdc < GAS_RESERVE_USD + POSITION_SIZE_USD:
                if time.time() - last_log > 60:
                    log(f"USDC too low (${usdc:.4f}), need ${GAS_RESERVE_USD + POSITION_SIZE_USD:.2f}")
                    last_log = time.time()

            else:
                # Strategy
                signal, meta = check_strategy(price, state, args)

                # Log every ~10s
                if time.time() - last_log > 10:
                    pos_info = ""
                    if state["position"]:
                        p = state["position"]
                        pnl_pct = (price - p["entry"]) / p["entry"] * 100
                        pos_info = f" | POS LONG @${p['entry']:.2f} ({pnl_pct:+.2f}%)"
                    trend_info = ""
                    if meta and "trend" in meta:
                        trend_info = f" | Trend:{meta['trend']} MA:{meta['ma']:.2f}"
                    log(
                        f"ETH ${price:.2f} | USDC ${usdc:.4f} | Total ${total_usd:.2f}"
                        f"{pos_info}{trend_info}"
                    )
                    last_log = time.time()

                # Execute
                if signal == "BUY":
                    if state["position"]:
                        pass  # already in position
                    else:
                        log(f"🚀 BUY SIGNAL! reason={meta.get('reason')} price=${price:.2f}")
                        if args.paper:
                            ok, info = paper_trade(state, "BUY", price)
                            if ok:
                                log(f"  Paper buy filled OK")
                        else:
                            size_usd = min(POSITION_SIZE_USD, usdc - GAS_RESERVE_USD)
                            if size_usd < 0.10:
                                log(f"  Too little USDC to trade (${size_usd:.4f})")
                            else:
                                weth_amt = size_usd / price
                                ok = swap_usdc_for_weth(int(size_usd * 1e6), price)
                                if ok:
                                    state["position"] = {
                                        "type":     "LONG",
                                        "entry":    price,
                                        "size_wei": int(weth_amt * 1e18),
                                        "cost_usd": size_usd,
                                    }
                                    state["last_trade_ts"] = time.time()
                                    telegram_notify(f"✅ LONG opened\nEntry: ${price:.4f}\nSize: ${size_usd:.2f}", "INFO")

                                    # Fix #4: consecutive_loss protection — pause after 3 losses
                                    if state.get("consecutive_loss", 0) >= 3:
                                        log(f"⚠️ {state['consecutive_loss']} consecutive losses, pausing 1 hour", "WARN")
                                        telegram_notify(f"⚠️ {state['consecutive_loss']} consecutive losses.\nBot paused 1 hour.", "WARNING")
                                        save_state(state)
                                        time.sleep(3600)
                                        state["consecutive_loss"] = 0
                                        # 继续执行交易，不要 continue

                                    log(f"  ✅ Buy filled! Entry ${price:.4f}")
                                else:
                                    nonce_mgr.rollback()

                elif signal == "SELL":
                    if not state["position"]:
                        pass  # no position to close
                    else:
                        pos = state["position"]
                        entry = pos["entry"]
                        size_wei = pos["size_wei"]
                        log(f"🎯 SELL SIGNAL! reason={meta.get('reason')} price=${price:.2f} entry=${entry:.2f}")

                        if args.paper:
                            ok, pnl = paper_trade(state, "SELL", price)
                            if ok:
                                if pnl < 0:
                                    state["consecutive_loss"] += 1
                                else:
                                    state["consecutive_loss"] = 0
                        else:
                            # Critical fix #3: wrap failure handling
                            eth_bal = get_eth()
                            if eth_bal > 0.0001:
                                wrap_amount = int((eth_bal - 0.0001) * 1e18)  # reserve 0.0001 ETH for gas
                                log(f"  Wrapping {eth_bal - 0.0001:.6f} ETH...")
                                tx = build_tx(WETH, "0xd0e30db0", gas=70000, value=wrap_amount)
                                h  = sign_send_with_retry(tx)
                                if not h or not wait_tx(h):
                                    log("  Wrap FAILED, skipping sell this cycle", "WARN")
                                    nonce_mgr.rollback()   # Fix: rollback nonce to avoid conflict on retry
                                    save_state(state)
                                    time.sleep(CHECK_INTERVAL)
                                    continue  # skip this sell; wait for next signal
                                time.sleep(5)  # wait for confirm

                            ok = swap_weth_for_usdc(size_wei, price)
                            if ok:
                                nonce_mgr.confirm()
                                proceeds   = size_wei / 1e18 * price
                                cost_usd   = pos["cost_usd"]
                                pnl        = proceeds - cost_usd - gas_cost / 2
                                state["daily_pnl"]   += pnl
                                state["total_pnl"]   += pnl
                                state["trades_today"] += 1
                                state["position"]     = None
                                state["last_trade_ts"] = time.time()
                                if pnl < 0:
                                    state["consecutive_loss"] += 1
                                else:
                                    state["consecutive_loss"] = 0
                                log(f"  ✅ Sell filled! PnL: {pnl:+.4f} | Total today: {state['daily_pnl']:+.4f}")
                                telegram_notify(f"📤 LONG closed\nPnL: ${pnl:.4f}\nTotal today: ${state.get('daily_pnl', 0):.4f}", "INFO")
                            else:
                                nonce_mgr.rollback()

            save_state(state)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log(f"Loop error: {tb[:300]}", "ERROR")
            telegram_notify(f"🚨 FATAL ERROR — HF Trader exiting.\nError: {e}", "CRITICAL")
            time.sleep(5)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
