#!/usr/bin/env bash
# ops/install.sh — one-shot setup for Theo on macOS.
#
# Idempotent: rerunning it refreshes the plist and seeds a healthy_commit
# when one is missing. Does NOT reinstall dependencies or run migrations —
# `just up && just migrate` remains a separate step so operators control
# timing.
#
# Usage:
#   ./ops/install.sh                   # install launchd agent only
#   ./ops/install.sh --with-observability   # also bring up the LGTM stack

set -euo pipefail

WORKSPACE="${THEO_WORKSPACE:-$HOME/Theo}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO_DIR/ops/com.theo.agent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.theo.agent.plist"

BUN_PATH="${BUN_PATH:-$(command -v bun || true)}"
if [ -z "$BUN_PATH" ]; then
  echo "Error: bun not found on PATH and BUN_PATH not set." >&2
  exit 1
fi

mkdir -p "$WORKSPACE/logs" "$WORKSPACE/data" "$WORKSPACE/config"

# Seed healthy_commit with the current HEAD when missing — first-run
# invariant. A broken update later is only recoverable when this file
# exists.
if [ ! -f "$WORKSPACE/data/healthy_commit" ]; then
  git -C "$REPO_DIR" rev-parse HEAD > "$WORKSPACE/data/healthy_commit"
  echo "Seeded healthy_commit: $(cat "$WORKSPACE/data/healthy_commit")"
fi

mkdir -p "$(dirname "$PLIST_DST")"
# sed's -E regex makes the three placeholder substitutions explicit; we
# escape / in the values via |.
sed \
  -e "s|__BUN_PATH__|$BUN_PATH|g" \
  -e "s|__REPO_DIR__|$REPO_DIR|g" \
  -e "s|__THEO_WORKSPACE__|$WORKSPACE|g" \
  "$PLIST_SRC" > "$PLIST_DST"

# Unload existing instance before loading the refreshed plist.
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load   "$PLIST_DST"

if [ "${1-}" = "--with-observability" ]; then
  (cd "$REPO_DIR/ops/observability" && docker compose up -d)
  echo "Grafana: http://localhost:3000 (admin/admin on first boot)"
fi

echo "Theo launchd agent installed."
echo "  workspace : $WORKSPACE"
echo "  logs      : $WORKSPACE/logs/"
echo "  stop      : launchctl unload $PLIST_DST"
