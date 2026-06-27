#!/bin/bash
# Usage: check_bot_alive.sh [max_age_seconds] [bot_name]
# Exit 0 = alive, Exit 1 = dead

MAX_AGE=${1:-180}
BOT=${2:-auto}

HEARTBEAT_FILE="/root/.openclaw/workspace/eth-grid-bot/data/.heartbeat_${BOT}"
LOG_FILE="/tmp/eth_trader.log"

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "[DEAD] No heartbeat file for $BOT"
    exit 1
fi

LAST=$(cat "$HEARTBEAT_FILE")
NOW=$(date +%s)
AGE=$((NOW - LAST))

if [ "$AGE" -gt "$MAX_AGE" ]; then
    echo "[DEAD] $BOT heartbeat age: ${AGE}s > ${MAX_AGE}s"
    exit 1
fi

echo "[ALIVE] $BOT: heartbeat ${AGE}s ago"
exit 0
