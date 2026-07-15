#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INTERVAL="${FEISHU_CODEX_WATCH_INTERVAL:-30}"
RUNNING_AFTER="${FEISHU_CODEX_RUNNING_HEARTBEAT_AFTER:-120}"
RUNNING_INTERVAL="${FEISHU_CODEX_RUNNING_HEARTBEAT_INTERVAL:-120}"

cd "$ROOT"
exec /usr/bin/env python3 "$ROOT/bin/feishu_codex_bridge.py" watch \
  --interval "$INTERVAL" \
  --running-heartbeat-after "$RUNNING_AFTER" \
  --running-heartbeat-interval "$RUNNING_INTERVAL" \
  "$@"
