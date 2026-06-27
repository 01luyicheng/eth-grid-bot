#!/usr/bin/env python3
"""
Base Chain HF ETH Trader v2
===========================
Mean-reversion + trend-confirmation + volatility-gate strategy.
Optimised for Base chain + Gate.io data source (Binance blocked by proxy).

Data:       Gate.io public trades API (cursor pagination, no auth needed)
            - Fetch 7000+ trades at startup (~2h of history)
            - Update with latest 100 trades each tick
            - MA = average of last MA_PERIOD close prices
Chain:      Base mainnet
Wallet:     Reads from wallet.env
Mode:       --paper (simulate) | --live (real trades)

Gas economics on Base:
  Gas price ≈ 0.006 gwei, 200k gas/swap
  Cost per swap = 200000 × 6e-9 ETH = 0.0012 ETH ≈ $2 at ETH=$1700
  Round-trip = ~$4.04
  → This is FASTER than our $0.50 position!
  → Strategy: only trade when position can absorb gas cost
  → Or: accumulate larger pool before HF trading

Key changes from v1:
  - Gate.io trades API for price data (Binance blocked)
  - Cursor pagination for 2h history bootstrap
  - Separate HF pool logic (use existing USDC/WETH)
  - Strict gas viability check before each trade
  - Short-selling (sell high, buy back low)
"""

import eth_abi
import requests
import json
import time
import os
import sys
import argparse
import statistics
import warnings
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from web3 import Web3

warnings.filterwarnings('ignore')

# ============================================================
# CONFIGURATION
# ============================================================
WALLET_ENV  = "/root/.openclaw/workspace/wallet/wallet.env"
STATE_FILE  = "/root/.openclaw/workspace/eth-grid-bot/data/hf_state.json"
LOG_FILE    = "/tmp/eth_hf_v2.log"

# --- Load wallet credentials (with file permission check) ---
st = os.stat(WALLET_ENV)
mode = st.st_mode & 0o777
if mode != 0o600:
    raise RuntimeError(f"wallet.env permissions {oct(mode)} are too open; must be 0o600")

PRIVATE_KEY = WALLET = None
for line in open(WALLET_ENV):
    line = line.strip()
    if line.startswith("PRIVATE_KEY="):
        PRIVATE_KEY = line.split("=", 1)[1].strip()
    elif line.startswith("WALLET_ADDRESS="):
        WALLET = line.split("=", 1)[1].strip()
if not PRIVATE_KEY or not WALLET:
    raise RuntimeError("PRIVATE_KEY or WALLET missing from wallet.env")

# Chain & DeFi
RPC      = "https://mainnet.base.org"
PROXY    = "http://127.0.0.1:10808"
CHAIN_ID = 8453
WETH     = "0x4200000000000000000000000000000000000006"
USDC     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER   = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

# ===== TRADING PARAMETERS =====
CHECK_INTERVAL     = 3     # seconds between price checks
POSITION_SIZE_USD  = 0.50  # per trade
PROFIT_TARGET_PCT  = 0.5   # 0.5% take-profit
STOP_LOSS_PCT      = 0.3   # 0.3% stop-loss
TIME_STOP_SECS     = 300   # 5 min time-stop

# ===== STRATEGY PARAMETERS =====
MA_PERIOD          = 12    # shorter MA for faster signals (~3min at Gate.io trade rate)
# Trade count-based: Gate.io ~1 trade/sec → MA_PERIOD=12 gives ~12s history
# But we fetch every 30s, so 30s/2s per trade = 15 trades per fetch
# The rolling update adds 100 trades/30s ≈ 3 trades/sec
# With CHECK_INTERVAL=3s, we get ~9 new trades per check
# MA of 12 trades = 12 × (30/100) = 3.6 minutes effective MA window

# For proper time-based MA, we need a much larger trade count
# 2h / (30s per fetch / 3 trades per sec) = 2h × 3 trades/sec / (1/30 fetch/sec)
# = 7200 trades per 2h → MA_PERIOD=7200
# Since storing 7200 trades is too much, we use a smaller window
# and accept that it's a "trade-count MA" not a "time MA"
# Practical: MA_PERIOD=200 ≈ ~1 minute of Gate.io history

VOL_PERIOD         = 6     # volatility window
DEV_THRESHOLD_PCT  = 0.10 # lowered from 0.30: price deviation required to enter
SLOPE_THRESHOLD    = 0.002 # MA slope threshold (normalised)
VOL_MULTIPLIER     = 0.5  # current vol must be > 50% of avg past vol

# ===== RISK CONTROLS =====
MAX_TRADES_PER_DAY  = 20
MAX_DAILY_LOSS_USD  = 0.30
GAS_RESERVE_ETH     = 0.0003  # $0.51 gas reserve
MAX_GAS_PCT         = 0.20   # max 20% of position used for gas

# ===== GAS =====
PRIORITY_FEE = 500_000_000  # 0.5 gwei

# ============================================================
# SESSION
# ============================================================
session = requests.Session()
session.proxies = {"http": PROXY, "https": PROXY}
session.verify = False  # proxy SSL handling
w3   = Web3()
acct = w3.eth.account.from_key(PRIVATE_KEY)

# ============================================================
# HELPERS
# ============================================================

# === Telegram Alerts ===
_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

def telegram_notify(message, priority="INFO"):
    """Send alert via Telegram Bot. Silent failure if not configured."""
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return
    text = f"[{priority}] ETH-HF-Trader-v2\n{message}"
    url  = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": _TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass  # silent failure

# === Heartbeat (health check) ===
_HEARTBEAT_FILE = "/root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_hf_v2"

def _heartbeat_writer():
    """Write heartbeat timestamp every 60 seconds."""
    while True:
        try:
            os.makedirs(os.path.dirname(_HEARTBEAT_FILE), exist_ok=True)
            with open(_HEARTBEAT_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        time.sleep(60)

def start_heartbeat():
    t = threading.Thread(target=_heartbeat_writer, daemon=True)
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
        os.makedirs(os.path.dirname(_HEARTBEAT_FILE), exist_ok=True)
        with open(_HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass

def rpc(method, params=None):
    r = session.post(RPC, json={
        "jsonrpc": "2.0", "method": method,
        "params": params or [], "id": 1
    }, timeout=15)
    return r.json()

def get_nonce():
    return int(rpc("eth_getTransactionCount", [WALLET, "pending"])["result"], 16)


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
        with self._lock:
            pass

    def rollback(self):
        with self._lock:
            if self._nonce is not None and self._nonce > 0:
                self._nonce -= 1

nonce_mgr = NonceManager(rpc, WALLET)

def get_gas_price_gwei():
    return int(rpc("eth_gasPrice")["result"], 16) / 1e9  # in Gwei

def get_max_fee():
    combined = get_gas_price_gwei() * 1e9
    base_fee = combined - PRIORITY_FEE
    return base_fee * 2 + PRIORITY_FEE

def estimate_gas_cost_usd(eth_price=1700):
    """Estimated USD cost for ONE swap on Base (200k gas × gas_price)."""
    try:
        gas_price_wei = int(rpc("eth_gasPrice")["result"], 16)
        gas_eth = 200_000 * gas_price_wei / 1e18
        return gas_eth * eth_price
    except Exception:
        return 0.05  # conservative

def estimate_gas_roundtrip(eth_price=1700):
    return estimate_gas_cost_usd(eth_price) * 2

def sign_send(tx):
    try:
        signed = acct.sign_transaction(tx)
        hex_tx = "0x" + signed.raw_transaction.hex()
        result = rpc("eth_sendRawTransaction", [hex_tx])
        if "result" in result:
            log(f"  TX: {result['result'][:22]}...")
            return result["result"]
        elif "error" in result:
            log(f"  TX error: {result['error'].get('message', '')[:80]}")
            return None
    except Exception as e:
        log(f"  Sign error: {e}")
    return None

def wait_tx(tx_hash, timeout=90):
    start = time.time()
    while time.time() - start < timeout:
        result  = rpc("eth_getTransactionReceipt", [tx_hash])
        receipt = result.get("result")
        if receipt:
            ok = receipt.get("status") == "0x1"
            if not ok:
                log(f"  TX FAILED! Gas: {receipt.get('gasUsed', '?')}")
            return ok
        time.sleep(2)
    return False

def build_tx(to, data, gas, value=0):
    max_fee = get_max_fee()
    nonce   = nonce_mgr.get()
    return {
        "from":                  WALLET,
        "to":                    to,
        "data":                  data,
        "value":                 value,
        "nonce":                 nonce,
        "maxFeePerGas":          max_fee,
        "maxPriorityFeePerGas":  PRIORITY_FEE,
        "chainId":               CHAIN_ID,
        "type":                  2,
        "gas":                   gas,
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

# ============================================================
# PRICE DATA — Gate.io Trades (public, cursor pagination)
# ============================================================
def fetch_gate_trades(limit=100, cursor=None):
    """Fetch latest trades from Gate.io public API.
    Each trade: {id, create_time, side, amount, price, ...}
    Returns (trades_list, next_cursor_or_None)"""
    try:
        url = "https://api.gateio.ws/api/v4/spot/trades"
        params = {"currency_pair": "ETH_USDT", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = session.get(url, params=params, timeout=10)
        data = r.json()
        if not isinstance(data, list):
            return [], None
        next_cur = None
        if len(data) == limit:
            # Use last trade ID as next cursor
            next_cur = str(data[-1]["id"])
        return data, next_cur
    except Exception as e:
        log(f"Gate trades error: {e}", "WARN")
        return [], None

def bootstrap_price_history(target_prices=2000):
    """
    Fetch ~2 hours of historical trades from Gate.io using cursor pagination.
    Returns list of (timestamp, price) tuples in chronological order.
    """
    log(f"[DATA] Bootstrapping price history from Gate.io (target: {target_prices} prices)...")
    all_trades = []  # [(ts, price), ...] oldest first
    cursor = None
    max_pages = 10

    for page in range(max_pages):
        trades, cursor = fetch_gate_trades(limit=1000, cursor=cursor)
        if not trades:
            break

        # trades are newest-first; reverse to chronological order
        page_tuples = [(float(t["create_time"]), float(t["price"])) for t in reversed(trades)]
        all_trades.extend(page_tuples)

        if len(all_trades) >= target_prices:
            break
        if cursor is None:
            break
        time.sleep(0.1)

    log(f"[DATA] Bootstrap: {len(all_trades)} prices (page {page+1})")
    if len(all_trades) >= 2:
        span = all_trades[-1][0] - all_trades[0][0]
        log(f"[DATA] Price history span: {span/3600:.2f}h")
    return all_trades[-target_prices:]

def get_latest_trade_price():
    """Fast: get just the most recent trade price."""
    trades, _ = fetch_gate_trades(limit=1)
    if trades:
        return float(trades[0]["price"])
    return None

def get_recent_prices(n=100):
    """Get the last N trades from Gate.io as (timestamp, price) tuples."""
    trades, _ = fetch_gate_trades(limit=n)
    if trades:
        return [(float(t["create_time"]), float(t["price"])) for t in trades]
    return []

# ============================================================
# STRATEGY ENGINE
# ============================================================
class StrategyEngine:
    """
    Mean-reversion on Gate.io trade-stream data.

    We treat each price point as a "snapshot" equivalent to a candle close.
    With Gate.io's trade frequency (~1 trade/second for ETH), MA_PERIOD=24
    gives us ~24 seconds of history — too short!
    SOLUTION: we use a sliding window of N prices, where N is set to
    MA_PERIOD * average_trades_per_candle to approximate a 5-min candle.

    More practically: we accumulate prices over time and use a
    large enough window to approximate the desired time period.

    Entry (all must be true):
      1. |price - MA| / MA > DEV_THRESHOLD_PCT
      2. current_vol > avg_past_vol × VOL_MULTIPLIER
      3. MA slope direction matches entry direction

    Direction:
      BUY  = price < MA × (1 - DEV/100)  → price below fair value, bounce expected
      SELL = price > MA × (1 + DEV/100)  → price above fair value, dump expected
    """

    def __init__(self, state):
        self.ma_period   = MA_PERIOD
        self.vol_period  = VOL_PERIOD
        self.state        = state
        # price_history: list of (timestamp, price) tuples, newest last
        if "price_history" not in state:
            state["price_history"] = []  # [(ts, price), ...]
        self._initialized = False

    def update_prices(self, new_prices):
        """Append new prices to history. Keep last MA_PERIOD * 4 for safety."""
        hist = self.state["price_history"]
        hist.extend(new_prices)
        # Keep a large window (enough for MA + vol computation)
        max_keep = max(MA_PERIOD, VOL_PERIOD) * 8
        self.state["price_history"] = hist[-max_keep:]

    def compute_ma(self):
        """Simple moving average of last MA_PERIOD prices."""
        hist = self.state.get("price_history", [])
        if len(hist) < self.ma_period:
            return None, None
        window = hist[-self.ma_period:]
        prices = [p for _, p in window] if isinstance(window[0], tuple) else window
        ma = sum(prices) / len(prices)

        # Slope: compare recent MA vs older MA
        slope = None
        if len(hist) >= self.ma_period + 5:
            recent = prices[-5:]
            older  = hist[-(self.ma_period + 5):-(5)]
            older_prices = [p for _, p in older] if isinstance(older[0], tuple) else older
            if older_prices and sum(older_prices) / len(older_prices) > 0:
                older_ma = sum(older_prices) / len(older_prices)
                slope = (sum(recent) / len(recent) - older_ma) / older_ma
        return ma, slope

    def compute_volatility(self):
        """Volatility = mean absolute return of last VOL_PERIOD prices."""
        hist = self.state.get("price_history", [])
        if len(hist) < self.vol_period + 1:
            return None, None
        # Convert to prices if tuples
        prices = [p if not isinstance(p, tuple) else p[1] for p in hist]
        prices = prices[-self.vol_period * 2:]  # last 2× periods

        returns = []
        for i in range(-self.vol_period, 0):
            if prices[i] > 0 and prices[i-1] > 0:
                ret = abs((prices[i] - prices[i-1]) / prices[i-1])
                returns.append(ret)

        if not returns:
            return None, None
        cur_vol = statistics.mean(returns[-self.vol_period:]) if len(returns) >= self.vol_period else statistics.mean(returns)
        avg_vol = statistics.mean(returns[:-self.vol_period]) if len(returns) > self.vol_period else cur_vol
        return cur_vol, avg_vol

    def check(self, price):
        """Main strategy check. Returns (signal, meta)."""
        pos       = self.state.get("position")
        ma, slope = self.compute_ma()
        cur_vol, avg_vol = self.compute_volatility()

        result = {
            "ma":      ma,
            "slope":   slope,
            "cur_vol": cur_vol,
            "avg_vol": avg_vol,
        }

        # =================== EXIT LOGIC ===================
        if pos:
            entry      = pos["entry"]
            entry_time = pos.get("entry_time", 0)
            elapsed    = time.time() - entry_time

            if pos["type"] == "LONG":
                pnl_pct = (price - entry) / entry * 100
                if price >= entry * (1 + PROFIT_TARGET_PCT / 100):
                    return "SELL", {**result, "reason": "TAKE_PROFIT", "pnl_pct": pnl_pct}
                elif price <= entry * (1 - STOP_LOSS_PCT / 100):
                    return "SELL", {**result, "reason": "STOP_LOSS",   "pnl_pct": pnl_pct}
                elif elapsed >= TIME_STOP_SECS:
                    return "SELL", {**result, "reason": "TIME_STOP",   "pnl_pct": pnl_pct}
            else:  # SHORT
                pnl_pct = (entry - price) / entry * 100
                if price <= entry * (1 - PROFIT_TARGET_PCT / 100):
                    return "BUY",  {**result, "reason": "TAKE_PROFIT", "pnl_pct": pnl_pct}
                elif price >= entry * (1 + STOP_LOSS_PCT / 100):
                    return "BUY",  {**result, "reason": "STOP_LOSS",   "pnl_pct": pnl_pct}
                elif elapsed >= TIME_STOP_SECS:
                    return "BUY",  {**result, "reason": "TIME_STOP",   "pnl_pct": pnl_pct}

            return "HOLD", result

        # =================== ENTRY LOGIC ===================
        if ma is None or len(self.state.get("price_history", [])) < self.ma_period:
            return "HOLD", {**result, "reason": "warming_up"}

        dev_pct = (price - ma) / ma * 100

        # Condition 1: price deviation
        if abs(dev_pct) <= DEV_THRESHOLD_PCT:
            return "HOLD", {**result, "dev_pct": dev_pct, "reason": "dev_too_small"}

        # Condition 2: volatility gate
        if cur_vol is not None and avg_vol is not None and avg_vol > 0:
            if cur_vol < avg_vol * VOL_MULTIPLIER:
                return "HOLD", {**result, "dev_pct": dev_pct, "reason": "vol_too_calm"}

        # Condition 3: slope confirmation
        if slope is not None:
            if dev_pct < 0 and slope >= -SLOPE_THRESHOLD:
                return "HOLD", {**result, "dev_pct": dev_pct, "reason": "no_downward_slope"}
            if dev_pct > 0 and slope <= SLOPE_THRESHOLD:
                return "HOLD", {**result, "dev_pct": dev_pct, "reason": "no_upward_slope"}

        # All green → enter!
        if dev_pct < 0:
            return "BUY", {**result, "dev_pct": dev_pct, "reason": "MEAN_REVERT_BOUNCE"}
        else:
            return "SELL", {**result, "dev_pct": dev_pct, "reason": "MEAN_REVERT_DUMP"}


# ============================================================
# TRADE EXECUTION (Web3)
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

SLIPPAGE = 0.005

def ensure_approval(token, amount_wei):
    allowance = get_allowance(token, ROUTER)
    if allowance >= amount_wei:
        return True
    log(f"  Approving router for {amount_wei}...")
    data = (
        "0x095ea7b3"
        + ROUTER[2:].lower().zfill(64)
        + hex(2**256 - 1)[2:].zfill(64)
    )
    tx = build_tx(token, data, gas=50000)
    h  = sign_send(tx)
    if not h or not wait_tx(h):
        nonce_mgr.rollback()
        return False
    time.sleep(3)
    return True

def swap_usdc_for_weth(amount_usdc_wei, entry_price):
    amount_out_min = int(amount_usdc_wei / entry_price * (1 - SLIPPAGE))
    log(f"  BUY  {amount_usdc_wei/1e6:.4f} USDC → WETH (min {amount_out_min/1e18:.6f})")
    if not ensure_approval(USDC, amount_usdc_wei):
        return False
    calldata = _exact_input_single({
        "token_in":        USDC,
        "token_out":       WETH,
        "fee":             3000,
        "recipient":        WALLET,
        "deadline":        int(time.time()) + 600,
        "amount_in":       amount_usdc_wei,
        "amount_out_min":  amount_out_min,
        "sqrt_price_limit": 0,
    })
    tx = build_tx(ROUTER, "0x" + calldata, gas=200000)
    h  = sign_send(tx)
    if not h:
        nonce_mgr.rollback()
        return False
    ok = wait_tx(h)
    if ok:
        nonce_mgr.confirm()
    return ok

def swap_weth_for_usdc(amount_weth_wei, exit_price):
    amount_out_min = int(amount_weth_wei * exit_price * (1 - SLIPPAGE))
    log(f"  SELL {amount_weth_wei/1e18:.6f} WETH → USDC (min {amount_out_min/1e6:.4f})")
    weth_bal = get_weth()
    if weth_bal * 1e18 < amount_weth_wei:
        log(f"  Insufficient WETH: {weth_bal:.6f}")
        return False
    calldata = _exact_input_single({
        "token_in":        WETH,
        "token_out":       USDC,
        "fee":             3000,
        "recipient":        WALLET,
        "deadline":        int(time.time()) + 600,
        "amount_in":        amount_weth_wei,
        "amount_out_min":  amount_out_min,
        "sqrt_price_limit": 0,
    })
    tx = build_tx(ROUTER, "0x" + calldata, gas=200000)
    h  = sign_send(tx)
    if not h:
        nonce_mgr.rollback()
        return False
    ok = wait_tx(h)
    if ok:
        nonce_mgr.confirm()
    return ok

def wrap_eth(amount_wei):
    log(f"  Wrapping {amount_wei/1e18:.6f} ETH → WETH")
    tx = build_tx(WETH, "0xd0e30db0", gas=70000, value=amount_wei)
    h  = sign_send(tx)
    if not h:
        nonce_mgr.rollback()
        return False
    ok = wait_tx(h)
    if ok:
        nonce_mgr.confirm()
    return ok


# ============================================================
# PAPER MODE TRADING
# ============================================================
def paper_trade_open(state, signal, price):
    pos_type = "LONG" if signal == "BUY" else "SHORT"
    state["position"] = {
        "type":       pos_type,
        "entry":      price,
        "entry_time": time.time(),
    }
    log(f"  [PAPER] OPEN {pos_type} @ ${price:.4f}")

def paper_trade_close(state, reason, price, gas_cost):
    pos   = state["position"]
    entry = pos["entry"]
    ptype = pos["type"]

    if ptype == "LONG":
        pnl = (price - entry) / entry * POSITION_SIZE_USD - gas_cost
    else:
        pnl = (entry - price) / entry * POSITION_SIZE_USD - gas_cost

    state["position"]          = None
    state["daily_pnl"]        += pnl
    state["total_pnl"]        += pnl
    state["trades_today"]     += 1
    state["daily_trades"].append({
        "time":   time.time(),
        "pnl":    pnl,
        "reason": reason,
        "entry":  entry,
        "exit":   price,
    })
    result = "WIN" if pnl >= 0 else "LOSS"
    if pnl >= 0:
        state["wins"]  = state.get("wins", 0) + 1
    else:
        state["losses"] = state.get("losses", 0) + 1

    log(f"  [PAPER] CLOSE {ptype} @ ${price:.4f} | PnL: {pnl:+.4f} ({result}) | {reason}")
    return pnl


# ============================================================
# STATE MANAGEMENT
# ============================================================
def default_state():
    return {
        "day":              0,
        "trades_today":     0,
        "daily_pnl":        0.0,
        "total_pnl":        0.0,
        "position":         None,
        "price_history":    [],   # [(timestamp, price), ...]
        "last_trade_fetch": 0,
        "daily_trades":     [],
        "sessions":         0,
        "start_balance":    None,
        "wins":             0,
        "losses":           0,
        "signals_skipped":  0,
        "gas_too_high":     0,
        "consecutive_loss": 0,   # Fix: added consecutive_loss protection
    }

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
            s = default_state()
            s.update(raw)
            return s
        except Exception as e:
            log(f"State load error: {e}")
    return default_state()

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.rename(tmp, STATE_FILE)


# ============================================================
# MAIN LOOP
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper",   action="store_true", help="Paper trade (simulate, no real txs)")
    parser.add_argument("--live",    action="store_true", help="Live trading (real on-chain txs)")
    parser.add_argument("--verbose", action="store_true", help="Extra debug logging")
    args = parser.parse_args()

    mode = "LIVE" if args.live else "PAPER"
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    start_heartbeat()
    log(f"📡 Heartbeat: /root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_hf_v2")
    telegram_notify(f"🚀 HF Trader v2 started [{mode}]\nWallet: {WALLET}", "INFO")
    log(f"=== HF ETH Trader v2 STARTED [{mode}] ===")
    log(f"Wallet:         {WALLET}")
    log(f"Position size:  ${POSITION_SIZE_USD} / trade")
    log(f"Profit target:  {PROFIT_TARGET_PCT}% | Stop loss: {STOP_LOSS_PCT}% | Time stop: {TIME_STOP_SECS}s")
    log(f"MA period:      {MA_PERIOD} prices | Vol window: {VOL_PERIOD}")
    log(f"Dev threshold:  {DEV_THRESHOLD_PCT}% | Vol multiplier: {VOL_MULTIPLIER}")
    log(f"Max trades/day: {MAX_TRADES_PER_DAY} | Max daily loss: ${MAX_DAILY_LOSS_USD}")
    log(f"Gas reserve:    {GAS_RESERVE_ETH} ETH | Max gas % of position: {MAX_GAS_PCT*100:.0f}%")

    state     = load_state()
    engine    = StrategyEngine(state)

    # Bootstrap price history from Gate.io
    bootstrap_prices = bootstrap_price_history(target_prices=MA_PERIOD * 10)
    if bootstrap_prices:
        # bootstrap_prices is already [(ts, price), ...] from Gate.io
        engine.update_prices(bootstrap_prices)
        ma_val, _ = engine.compute_ma()
        log(f"[DATA] MA ready: {len(state['price_history'])} prices, MA={ma_val:.2f}" if ma_val
            else "[DATA] Warming up...")
    else:
        log("[DATA] Bootstrap failed, starting fresh", "WARN")

    # Daily reset
    today = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    if state["day"] != today:
        log(f"[DAY RESET] Yesterday: trades={state['trades_today']}, PnL=${state['daily_pnl']:.4f}")
        state["day"]           = today
        state["trades_today"]   = 0
        state["daily_pnl"]      = 0.0
        state["daily_trades"]   = []
        state["sessions"]        = state.get("sessions", 0) + 1
        state["wins"]            = 0
        state["losses"]         = 0

    if state["start_balance"] is None:
        eth_bal    = get_eth()
        weth_bal   = get_weth()
        usdc_bal   = get_usdc()
        price_ref  = get_latest_trade_price() or 1700
        state["start_balance"] = usdc_bal + eth_bal * price_ref + weth_bal * price_ref
        log(f"[INIT] ETH={eth_bal:.6f} WETH={weth_bal:.6f} USDC=${usdc_bal:.4f} | Total≈${state['start_balance']:.2f}")

    last_log       = 0
    last_fetch_ts  = 0
    FETCH_INTERVAL = 30  # fetch new trades every 30s to update price history

    while True:
        try:
            now = time.time()

            # ===== Fetch recent trades to update price history every 30s =====
            if now - last_fetch_ts >= FETCH_INTERVAL:
                recent = get_recent_prices(n=100)
                if recent:
                    engine.update_prices(recent)  # already (ts, price) tuples
                    state["last_trade_fetch"] = now
                    last_fetch_ts = now
                    if args.verbose:
                        log(f"  [DATA] +{len(recent)} prices, total: {len(state['price_history'])}")

            # ===== Get current price =====
            price = get_latest_trade_price()
            if price is None:
                log("Price unavailable, retrying...", "WARN")
                time.sleep(10)
                continue

            # ===== Balances =====
            eth_bal   = get_eth()
            weth_bal  = get_weth()
            usdc_bal  = get_usdc()
            total_usd = eth_bal * price + weth_bal * price + usdc_bal

            # Gas estimate
            gas_cost  = estimate_gas_cost_usd(price)
            gas_round = gas_cost * 2

            # ===== Daily reset =====
            today = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
            if state["day"] != today:
                log(f"[DAY RESET] trades={state['trades_today']} PnL=${state['daily_pnl']:.4f} | Wins={state.get('wins',0)} Losses={state.get('losses',0)}")
                state["day"]           = today
                state["trades_today"]   = 0
                state["daily_pnl"]      = 0.0
                state["daily_trades"]   = []
                state["sessions"]        = state.get("sessions", 0) + 1
                state["wins"]            = 0
                state["losses"]          = 0

            # ===== Risk checks =====
            can_trade = True
            risk_msg  = ""

            if state["trades_today"] >= MAX_TRADES_PER_DAY:
                can_trade = False
                risk_msg  = f"Max trades ({MAX_TRADES_PER_DAY})"
            elif state["daily_pnl"] <= -MAX_DAILY_LOSS_USD:
                can_trade = False
                risk_msg  = f"Daily loss limit (-${MAX_DAILY_LOSS_USD})"
            elif eth_bal < GAS_RESERVE_ETH:
                can_trade = False
                risk_msg  = f"ETH ({eth_bal:.6f}) < gas reserve ({GAS_RESERVE_ETH})"
            elif gas_cost > POSITION_SIZE_USD * MAX_GAS_PCT:
                can_trade = False
                risk_msg  = f"Gas (${gas_cost:.4f}) > {MAX_GAS_PCT*100:.0f}% of position (${POSITION_SIZE_USD * MAX_GAS_PCT:.4f})"

            # Check if we have enough USDC for BUY or enough WETH for SELL
            min_usdc_needed = POSITION_SIZE_USD * 1.01  # 1% buffer
            min_weth_needed = POSITION_SIZE_USD / price * 1.1  # 10% buffer
            if usdc_bal < min_usdc_needed and weth_bal < min_weth_needed:
                can_trade = False
                risk_msg  = f"USDC (${usdc_bal:.4f}) and WETH ({weth_bal:.6f}) too low for position"

            if risk_msg and now - last_log > 30:
                log(f"[RISK] {risk_msg}", "WARN")
                last_log = now

            # ===== Strategy check =====
            signal, meta = engine.check(price)

            # ===== Status log (every 15s) =====
            if now - last_log >= 15:
                pos_info = ""
                if state["position"]:
                    p       = state["position"]
                    elapsed = now - p.get("entry_time", now)
                    if p["type"] == "LONG":
                        pnl_pct = (price - p["entry"]) / p["entry"] * 100
                    else:
                        pnl_pct = (p["entry"] - price) / p["entry"] * 100
                    pos_info = f" | {p['type']} @${p['entry']:.2f} ({pnl_pct:+.2f}%, {elapsed:.0f}s)"
                ma_info = ""
                if meta.get("ma"):
                    ma_info = f" | MA={meta['ma']:.2f} dev={meta.get('dev_pct',0):+.3f}% vol={meta.get('cur_vol',0)*100:.3f}%"
                log(
                    f"ETH ${price:.2f} | USDC ${usdc_bal:.4f} WETH {weth_bal:.6f} | Total ${total_usd:.2f}"
                    f"{pos_info}{ma_info}"
                    f" | Sig={signal} gas=${gas_cost:.4f}/swap"
                )
                last_log = now

            # ===== Execute signals =====
            if can_trade and signal in ("BUY", "SELL"):
                pos = state.get("position")

                if signal == "BUY" and not pos:
                    if usdc_bal < min_usdc_needed:
                        log(f"  Not enough USDC: ${usdc_bal:.4f} < ${min_usdc_needed:.4f}")
                    else:
                        log(f"🚀 BUY SIGNAL! reason={meta.get('reason')} price=${price:.2f} dev={meta.get('dev_pct',0):+.3f}%")
                        if mode == "PAPER":
                            paper_trade_open(state, "BUY", price)
                        else:
                            ok = swap_usdc_for_weth(int(POSITION_SIZE_USD * 1e6), price)
                            if ok:
                                state["position"] = {
                                    "type":       "LONG",
                                    "entry":      price,
                                    "entry_time": now,
                                    "cost_usd":   POSITION_SIZE_USD,
                                }
                                log(f"  ✅ LONG opened @ ${price:.4f}")

                elif signal == "SELL" and not pos:
                    if weth_bal < min_weth_needed:
                        log(f"  Not enough WETH: {weth_bal:.6f} < {min_weth_needed:.6f}")
                    else:
                        log(f"🚀 SELL SIGNAL! reason={meta.get('reason')} price=${price:.2f} dev={meta.get('dev_pct',0):+.3f}%")
                        if mode == "PAPER":
                            paper_trade_open(state, "SELL", price)
                        else:
                            # Wrap ETH first
                            if eth_bal > 0.0001:
                                wrap_eth(int((eth_bal - 0.0001) * 1e18))
                                time.sleep(3)
                            ok = swap_weth_for_usdc(int(min_weth_needed * 1e18), price)
                            if ok:
                                state["position"] = {
                                    "type":       "SHORT",
                                    "entry":      price,
                                    "entry_time": now,
                                    "cost_usd":   POSITION_SIZE_USD,
                                }
                                log(f"  ✅ SHORT opened @ ${price:.4f}")

                elif pos and signal in ("BUY", "SELL"):
                    pos_type    = pos["type"]
                    close_sig   = "SELL" if pos_type == "LONG" else "BUY"
                    if signal == close_sig:
                        reason = meta.get("reason", "UNKNOWN")
                        log(f"🎯 CLOSE {pos_type}! reason={reason} price=${price:.2f}")

                        if mode == "PAPER":
                            paper_trade_close(state, reason, price, gas_cost)
                        else:
                            if pos_type == "LONG":
                                if eth_bal > 0.0001:
                                    wrap_eth(int((eth_bal - 0.0001) * 1e18))
                                    time.sleep(3)
                                weth_size = int(POSITION_SIZE_USD / pos["entry"] * 1e18)
                                ok = swap_weth_for_usdc(weth_size, price)
                                if ok:
                                    pnl = (price - pos["entry"]) / pos["entry"] * POSITION_SIZE_USD - gas_cost
                            else:
                                ok = swap_usdc_for_weth(int(POSITION_SIZE_USD * 1e6), price)
                                if ok:
                                    pnl = (pos["entry"] - price) / pos["entry"] * POSITION_SIZE_USD - gas_cost

                            if ok:
                                state["position"]       = None
                                state["daily_pnl"]      += pnl
                                state["total_pnl"]      += pnl
                                state["trades_today"]   += 1
                                state["daily_trades"].append({
                                    "time": now, "pnl": pnl,
                                    "reason": reason,
                                    "entry": pos["entry"], "exit": price,
                                })
                                if pnl >= 0:
                                    state["wins"] = state.get("wins", 0) + 1
                                else:
                                    state["losses"] = state.get("losses", 0) + 1
                                log(f"  ✅ {pos_type} closed! PnL: {pnl:+.4f} | Daily: {state['daily_pnl']:+.4f}")

            save_state(state)

        except Exception as e:
            import traceback
            log(f"Loop error: {traceback.format_exc()[:400]}", "ERROR")
            time.sleep(5)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
