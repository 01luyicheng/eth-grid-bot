## 2026-10-18 - /tmp/ Directory Usage and Symlink Vulnerability

**Vulnerability:** Code heavily used hardcoded paths in the global `/tmp/` directory for logs, lock files, and heartbeat indicators. This allows malicious local users to create symlinks matching those paths prior to execution, tricking the script into overwriting critical system files with its own data or changing ownership of system files.

**Learning:** Global world-writable directories (like `/tmp/`) should never be used to store application-specific data unless handled via secure temporary file libraries that ensure atomicity and correct permission. Hardcoding a path in `/tmp/` opens the application up to race conditions and symlink attacks.

**Prevention:** Move all data storage and temporary files into a project-local or user-specific directory (e.g., `./data/`). Always ensure directories used by the application are securely permissions-controlled and are isolated from the rest of the system's users.
