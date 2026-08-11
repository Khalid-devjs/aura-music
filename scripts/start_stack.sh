#!/bin/bash
# Aura Music full-stack launcher — starts every component of the
# clean-IP download infrastructure in order. Safe to re-run (idempotent).
#
# Components:
#   1. POT server       (Deno, :4416) — mints PO tokens for local yt-dlp
#   2. File-drop server (Python, :9090) — receives mp3s from the Kernel VM
#   3. Kernel tunnel    (kernel browsers ssh -R) — VM -> localhost bridge
#   4. Music bot        (venv python main.py)
#
# Usage: bash scripts/start_stack.sh   (env vars come from /root/musicbot/.env)

set -e
cd "$(dirname "$0")/.."

# Load .env for KERNEL_SESSION_ID / KERNEL_DROP_TOKEN / MCP_KERNEL_API_KEY
set -a
# shellcheck disable=SC1091
source .env 2>/dev/null || true
set +a

is_running() { pgrep -f "$1" >/dev/null 2>&1; }

# 1) POT server
if is_running "deno run --allow-all src/main.ts -p 4416"; then
  echo "[stack] POT server already running"
else
  echo "[stack] starting POT server…"
  (cd /tmp/bgutil-ytdlp-pot-provider/server && nohup /root/.deno/bin/deno run --allow-all src/main.ts -p 4416 > /tmp/pot-server.log 2>&1 &)
  sleep 2
fi

# 2) File-drop server
if is_running "file_drop_server.py"; then
  echo "[stack] file-drop server already running"
else
  echo "[stack] starting file-drop server…"
  KERNEL_DROP_TOKEN="${KERNEL_DROP_TOKEN:?KERNEL_DROP_TOKEN not set}" \
    nohup venv/bin/python file_drop_server.py 9090 > logs/file_drop.log 2>&1 &
  sleep 1
fi

# 3) Kernel tunnel (only if KEY + session configured)
if [ -n "${KERNEL_SESSION_ID:-}" ]; then
  if is_running "kernel browsers ssh ${KERNEL_SESSION_ID}"; then
    echo "[stack] kernel tunnel already running"
  else
    echo "[stack] starting kernel tunnel…"
    export KERNEL_API_KEY
    (sleep 86400 | nohup kernel browsers ssh "$KERNEL_SESSION_ID" \
      -R 4416:127.0.0.1:4416 -R 9090:127.0.0.1:9090 > logs/kernel_tunnel.log 2>&1 &)
    sleep 6
  fi
else
  echo "[stack] KERNEL_SESSION_ID not set — skipping tunnel"
fi

# 4) Bot
if is_running "venv/bin/python main.py"; then
  echo "[stack] bot already running"
else
  echo "[stack] starting bot…"
  nohup venv/bin/python main.py > logs/musicbot.log 2>&1 &
fi

echo "[stack] done. Components:"
pgrep -af "deno run --allow-all src/main.ts|file_drop_server|kernel browsers ssh|venv/bin/python main.py" | grep -v grep || true