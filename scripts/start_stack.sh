#!/bin/bash
# Aura Music watchdog — keeps the full stack alive. Run via:
#   nohup bash scripts/start_stack.sh --watchdog > logs/watchdog.log 2>&1 &
# Every 15s it checks each component and restarts anything that died.

cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env 2>/dev/null || true
set +a

is_running() { pgrep -f "$1" >/dev/null 2>&1; }

start_pot() {
  if is_running "deno run --allow-all src/main.ts -p 4416"; then return 0; fi
  echo "[$(date +%T)] starting POT server"
  (cd /tmp/bgutil-ytdlp-pot-provider/server && nohup /root/.deno/bin/deno run --allow-all src/main.ts -p 4416 > /tmp/pot-server.log 2>&1 &)
}

start_filedrop() {
  if is_running "file_drop_server.py"; then return 0; fi
  echo "[$(date +%T)] starting file-drop server"
  KERNEL_DROP_TOKEN="${KERNEL_DROP_TOKEN:?KERNEL_DROP_TOKEN not set}" \
    nohup venv/bin/python file_drop_server.py 9090 > logs/file_drop.log 2>&1 &
}

start_tunnel() {
  [ -n "${KERNEL_SESSION_ID:-}" ] || return 0
  if is_running "kernel browsers ssh ${KERNEL_SESSION_ID}"; then return 0; fi
  echo "[$(date +%T)] starting kernel tunnel"
  # kernel CLI reads KERNEL_API_KEY; the MCP key is stored as MCP_KERNEL_API_KEY
  export KERNEL_API_KEY="${KERNEL_API_KEY:-${MCP_KERNEL_API_KEY:-}}"
  if [ -z "$KERNEL_API_KEY" ]; then
    echo "[$(date +%T)] WARNING: no KERNEL_API_KEY — tunnel will fail auth"
  fi
  (sleep 86400 | nohup kernel browsers ssh "$KERNEL_SESSION_ID" \
    -R 4416:127.0.0.1:4416 -R 9090:127.0.0.1:9090 > logs/kernel_tunnel.log 2>&1 &)
}

start_bot() {
  if is_running "venv/bin/python main.py"; then return 0; fi
  echo "[$(date +%T)] starting bot"
  nohup venv/bin/python main.py >> logs/musicbot.log 2>&1 &
}

start_all() {
  start_pot
  start_filedrop
  start_tunnel
  start_bot
}

if [ "${1:-}" = "--watchdog" ]; then
  echo "[$(date +%T)] watchdog started"
  while true; do
    start_all
    sleep 15
  done
else
  start_all
  echo "[stack] started. Components:"
  pgrep -af "deno run --allow-all src/main.ts|file_drop_server|kernel browsers ssh|venv/bin/python main.py" | grep -v grep || true
fi