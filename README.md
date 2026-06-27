# ETH Grid Trader — Base Chain

一个运行在 **Base 链** 上的 ETH/USDC 自动化网格交易机器人。

**注意：** 本代码仅供研究学习，使用前请充分了解风险。

---

## 功能特性

- **动态网格** — 基于启动时价格自动计算 ±10% 网格（5层买卖）
- **跟踪止盈** — 价格突破 $1900 后开启 3% trailing stop
- **动态止损** — 止损线随网格下沿自动计算（网格底 -5%）
- **单实例锁** — 防止多开导致 nonce 冲突
- **Pending Tx 追踪** — 记录每笔交易状态，防止重复发送
- **指数退避重试** — RPC 调用失败自动重试（最多3次）
- **Trailing Stop 重入保护** — 交易 pending 失败不丢失保护状态
- **可配置滑点保护** — 支持三种写法（0.5 / 5 / 0.005）
- **RSI / ATR 支持**（可选增强）

---

## 快速开始

### 1. 安装依赖

```bash
pip install web3 eth_abi requests
```

### 2. 配置

```bash
cp wallet.env.example wallet.env
# 编辑 wallet.env，填入你的私钥
nano wallet.env
```

### 3. 运行

```bash
python3 eth_auto_trader.py
```

---

## 交易逻辑

```
价格下跌 → 触发网格 BUY 档 → 积累 ETH
价格上涨 → 触发网格 SELL 档 → 卖出 ETH 获利
价格跌破止损线 → 全仓止损
价格突破 $1900 → 开启跟踪止盈（从高点回落 3% 触发）
```

### 网格示意图

```
价格
  ^
$1705 | ─ ─ ─ SELL level 5 ─ ─ ─
$1628 | ─ ─ ─ SELL level 4
$1551 | ====== GRID CENTER ====== (启动时价格)
$1474 | ─ ─ ─ BUY level 1
$1397 | ─ ─ ─ BUY level 2
$1320 | ─ ─ ─ BUY level 3
$1243 | ─ ─ ─ BUY level 4
$1166 | ─ ─ ─ BUY level 5
$1115 | ═══════════════════════ 止损线 (网格底 -5%)
```

---

## 配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `GRID_CENTER` | `auto` | `auto` 或具体数字如 `2000` |
| `GRID_RANGE_PCT` | `10` | 网格上下各覆盖 % |
| `GRID_TIERS` | `5` | 档位数 |
| `SWAP_AMOUNT_WETH` | `0.0002` | 每次交易量（ETH）|
| `STOP_LOSS_BUFFER_PCT` | `5` | 网格底下方 % 触发止损 |
| `TRADE_COOLDOWN` | `120` | 交易冷却（秒）|
| `CHECK_INTERVAL` | `10` | 价格检查间隔（秒）|
| `SLIPPAGE` | `0.5` | 滑点保护（支持 0.5 / 5 / 0.005）|
| `TRAIL_TRIGGER_PRICE` | `1900` | 开启 trailing stop 的价格 |
| `MIN_BALANCE_ETH` | `0.001` | ETH 最低余额 |

---

## 安全建议

- **资金控制** — 建议初始资金 0.01 ETH + $50 USDC 起
- **私钥安全** — `wallet.env` 设置 `chmod 600`
- **监控日志** — 定期检查 `/tmp/eth_trader.log`
- **单机运行** — 禁止同时运行多个实例
- **网络隔离** — 建议通过代理运行，避免 IP 泄露

---

## 合约地址（Base 主网）

| 合约 | 地址 |
|------|------|
| WETH | `0x4200000000000000000000000000000000000006` |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Uniswap V3 Router | `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` |

---

## ⚠️ 风险提示

1. **智能合约风险** — 机器人需授权 USDC/WETH，可能被恶意合约盗用
2. **MEV 攻击** — 大额交易可能被 MEV 机器人夹，保留足够滑点保护
3. **网格局限性** — 单边行情中可能持续亏损
4. **Gas 波动** — Base 链 Gas 波动可能影响交易成功率
5. **本代码无任何盈利保证**，使用需自行承担风险

---

## 项目结构

```
eth-grid-bot/
├── eth_auto_trader.py     # 主交易脚本
├── wallet.env.example      # 配置示例
├── .gitignore
└── README.md
```

## License

MIT — 仅供研究学习使用
