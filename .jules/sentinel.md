## 2026-06-19 - Symlink Attacks via Global Directories
**Vulnerability:** Log files and lock files were stored in the global `/tmp/` directory across multiple trader scripts (`eth_auto_trader.py`, `eth_hf_trader.py`, `eth_hf_trader_v2.py`).
**Learning:** Using predictable file paths in world-writable directories like `/tmp/` exposes the application to symlink attacks. An attacker can create a symlink at the expected path pointing to a critical system file (e.g., `/etc/passwd`), causing the application to overwrite or corrupt it when it tries to write its logs or lock files.
**Prevention:** Store application logs, lock files, and state data in application-specific, securely permissioned local directories (e.g., `./data/`) instead of global temporary directories.
