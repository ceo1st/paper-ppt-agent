#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$ROOT/frontend"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv was not found in the current shell environment."
  echo "Install uv or open the shell where uv works, then run sh start-dev.sh again."
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm was not found in the current shell environment."
  echo "Install Node.js/npm or open the shell where npm works, then run sh start-dev.sh again."
  exit 1
fi

echo "==> Syncing backend dependencies with uv"
cd "$ROOT"
uv sync --locked

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "==> Installing frontend dependencies"
  cd "$FRONTEND_DIR"
  npm install
fi

echo "==> Starting Paper PPT Agent in this terminal"
cd "$ROOT"
PYTHONUNBUFFERED=1 uv run python -m backend.dev_launcher
