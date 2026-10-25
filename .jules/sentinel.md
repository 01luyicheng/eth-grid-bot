## 2026-10-25 - Security fixes for ETH Trading Bot

**Vulnerability:**
1. Potential Server-Side Request Forgery (SSRF) and scheme misuse when using `urllib.request.urlopen` (Bandit B310).
2. Predictable and shared `/tmp/` directory locations used for lock files, log files, and data directories (Bandit B108), which opens up the system to symlink attacks.
3. Silent exception swallowing in network and file operations (Bandit B110).
4. Code duplication in `telegram_notify` and heartbeat functionalities across multiple bot scripts (SonarCloud Code Duplication).

**Learning:**
1. `requests` library should be favored over `urllib` as it is generally safer by default and does not natively support `file://` schemes.
2. Temporary files and locks should be stored in predictable, bot-isolated directories (like workspace `./data/` directories) rather than the global `/tmp/` directory where other users or processes have write access.
3. Catching `Exception` and calling `pass` hides critical runtime errors and creates fragile code. Exceptions must be explicitly handled, or at least logged for auditing.
4. Duplicated code leads to fragmented maintainability. Centralizing common tasks into utility files ensures fixes apply globally.

**Prevention:**
1. Use `requests` instead of `urllib.request.urlopen` for all HTTP calls.
2. Store log files and lock files in the bot's local `data` directory (e.g. `/root/.openclaw/workspace/eth-grid-bot/data/`).
3. Explicitly log exceptions with details (e.g. `except Exception as e: log(f"Error: {e}")`).
4. Abstract repeated patterns (like heartbeat or notifications) to centralized helper files like `bot_utils.py` to prevent logic divergence and satisfy CI duplicate code checks.
