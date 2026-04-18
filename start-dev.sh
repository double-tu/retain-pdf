#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
FRONTEND_DIR="$ROOT_DIR/frontend"
BACKEND_DIR="$ROOT_DIR/backend/rust_api"
AUTH_FILE="$BACKEND_DIR/auth.local.json"
FRONTEND_RUNTIME_LOCAL="$FRONTEND_DIR/runtime-config.local.js"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-41000}"
SIMPLE_PORT="${SIMPLE_PORT:-42000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TYPST_BIN="${TYPST_BIN:-}"
UPLOAD_MAX_MB="${UPLOAD_MAX_MB:-500}"
UPLOAD_MAX_PAGES="${UPLOAD_MAX_PAGES:-500}"
RUST_API_UPLOAD_MAX_BYTES="${RUST_API_UPLOAD_MAX_BYTES:-$((UPLOAD_MAX_MB * 1024 * 1024))}"
RUST_API_UPLOAD_MAX_PAGES="${RUST_API_UPLOAD_MAX_PAGES:-$UPLOAD_MAX_PAGES}"

mkdir -p "$RUN_DIR"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

require_cmd cargo
require_cmd "$PYTHON_BIN"
require_cmd npm

if [[ -z "$TYPST_BIN" ]]; then
  if command -v typst >/dev/null 2>&1; then
    TYPST_BIN="$(command -v typst)"
  fi
fi

if [[ -z "$TYPST_BIN" ]]; then
  echo "typst not found" >&2
  echo "install it first, for example on macOS: brew install typst" >&2
  echo "or start with: TYPST_BIN=/absolute/path/to/typst ./start-dev.sh" >&2
  exit 1
fi

if [[ ! -x "$TYPST_BIN" ]]; then
  echo "typst is not executable: $TYPST_BIN" >&2
  exit 1
fi

if [[ ! -f "$AUTH_FILE" ]]; then
  echo "missing $AUTH_FILE" >&2
  echo "copy backend/rust_api/auth.local.example.json to backend/rust_api/auth.local.json first" >&2
  exit 1
fi

API_KEY="$("$PYTHON_BIN" -c 'import json,sys; data=json.load(open(sys.argv[1], "r", encoding="utf-8")); keys=data.get("api_keys") or []; print((keys[0] if keys else "").strip())' "$AUTH_FILE")"

if [[ -z "$API_KEY" ]]; then
  echo "no api_keys found in $AUTH_FILE" >&2
  exit 1
fi

cat > "$FRONTEND_RUNTIME_LOCAL" <<EOF
window.__FRONT_RUNTIME_CONFIG__ = {
  ...(window.__FRONT_RUNTIME_CONFIG__ || {}),
  apiBase: "http://$API_HOST:$API_PORT",
  xApiKey: "$API_KEY",
  mineruToken: "",
  modelApiKey: "",
  model: "",
  baseUrl: "",
  frontMaxBytes: $RUST_API_UPLOAD_MAX_BYTES,
  frontMaxPageCount: $RUST_API_UPLOAD_MAX_PAGES,
};
EOF

ensure_frontend_dependency() {
  local dependency_path="$1"
  if [[ -f "$FRONTEND_DIR/$dependency_path" ]]; then
    return
  fi
  echo "frontend dependency missing: $dependency_path"
  echo "installing frontend dependencies with npm install..."
  (
    cd "$FRONTEND_DIR"
    npm install
  )
}

ensure_frontend_dependency "node_modules/pdfjs-dist/build/pdf.mjs"

cleanup() {
  local exit_code=$?
  if [[ -f "$BACKEND_PID_FILE" ]]; then
    kill "$(cat "$BACKEND_PID_FILE")" >/dev/null 2>&1 || true
    rm -f "$BACKEND_PID_FILE"
  fi
  if [[ -f "$FRONTEND_PID_FILE" ]]; then
    kill "$(cat "$FRONTEND_PID_FILE")" >/dev/null 2>&1 || true
    rm -f "$FRONTEND_PID_FILE"
  fi
  exit "$exit_code"
}

trap cleanup INT TERM EXIT

if lsof -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $API_PORT is already in use" >&2
  exit 1
fi

if lsof -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $FRONTEND_PORT is already in use" >&2
  exit 1
fi

echo "writing logs to:"
echo "  backend:  $BACKEND_LOG"
echo "  frontend: $FRONTEND_LOG"
echo "  typst:    $TYPST_BIN"
echo "  limits:   ${RUST_API_UPLOAD_MAX_BYTES} bytes / ${RUST_API_UPLOAD_MAX_PAGES} pages"

(
  cd "$BACKEND_DIR"
  export RUST_API_BIND_HOST="$API_HOST"
  export RUST_API_PORT="$API_PORT"
  export RUST_API_SIMPLE_PORT="$SIMPLE_PORT"
  export TYPST_BIN
  export RUST_API_UPLOAD_MAX_BYTES
  export RUST_API_UPLOAD_MAX_PAGES
  exec cargo run
) >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$BACKEND_PID_FILE"

for _ in $(seq 1 60); do
  if lsof -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    echo "backend exited unexpectedly, check $BACKEND_LOG" >&2
    exit 1
  fi
  sleep 1
done

if ! lsof -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "backend did not become ready on port $API_PORT, check $BACKEND_LOG" >&2
  exit 1
fi

(
  cd "$FRONTEND_DIR"
  exec "$PYTHON_BIN" -m http.server "$FRONTEND_PORT" --bind "$FRONTEND_HOST"
) >"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!
echo "$FRONTEND_PID" > "$FRONTEND_PID_FILE"

for _ in $(seq 1 15); do
  if lsof -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    echo "frontend exited unexpectedly, check $FRONTEND_LOG" >&2
    exit 1
  fi
  sleep 1
done

if ! lsof -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "frontend did not become ready on port $FRONTEND_PORT, check $FRONTEND_LOG" >&2
  exit 1
fi

echo
echo "RetainPDF is running:"
echo "  frontend: http://$FRONTEND_HOST:$FRONTEND_PORT"
echo "  backend:  http://$API_HOST:$API_PORT/health"
echo
echo "press Ctrl+C to stop both services"

wait "$BACKEND_PID" "$FRONTEND_PID"
