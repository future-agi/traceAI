#!/usr/bin/env bash
# End-to-end run of traceAI-openai-realtime against a Future AGI collector.
#
# Needs FI_API_KEY and FI_SECRET_KEY (plus FI_BASE_URL for a self-hosted or local collector).
#   usage: [PYTHON=python3.12] [STANDIN=1] examples/run_e2e.sh [project-name] [openai version]
# Python 3.10-3.13 (fi-instrumentation-otel declares <3.14).
#
# With OPENAI_API_KEY set (and STANDIN unset) it runs short sessions against the live OpenAI
# Realtime API. That spends OpenAI credits: a typed question with a tool call and a spoken answer,
# the same with content capture off, and a text-only session; plus one refused connection.
#
# Without OPENAI_API_KEY (or with STANDIN=1) it starts examples/realtime_stub_server.py, a local
# STAND-IN that speaks the documented Realtime events with scripted answers. It is not OpenAI.
# Then it also runs the scenarios the live API cannot script: a spoken call with barge-in and
# error events, and a socket drop mid-response.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$(dirname "$HERE")"
PROJECT="${1:-openai-realtime-e2e}"
OPENAI_VERSION="${2:-3.31.0}"
WORK="$(mktemp -d)"
PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT

"${PYTHON:-python3}" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" -q install "$PKG" "openai[realtime]==$OPENAI_VERSION"
PY="$WORK/venv/bin/python"
EX="$HERE/voice_agent.py"
export FI_PROJECT_NAME="$PROJECT"
SHOWN="FI-MARK-shown$RANDOM$RANDOM"
HIDDEN="FI-MARK-hidden$RANDOM$RANDOM"

free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
start_stub() {  # start_stub <port> [stub args...]
  local port="$1"; shift
  "$PY" "$HERE/realtime_stub_server.py" --port "$port" "$@" >"$WORK/stub-$port.log" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 50); do grep -q "STAND-IN" "$WORK/stub-$port.log" 2>/dev/null && return 0; sleep 0.1; done
  echo "stand-in server did not start"; cat "$WORK/stub-$port.log"; exit 1
}

if [ -z "${OPENAI_API_KEY:-}" ] || [ "${STANDIN:-0}" = "1" ]; then
  echo "STAND-IN: no live OpenAI. Sessions go to examples/realtime_stub_server.py on 127.0.0.1."
  MAIN="$(free_port)"; start_stub "$MAIN"
  DROP="$(free_port)"; start_stub "$DROP" --drop-after-deltas 4
  export OPENAI_API_KEY="sk-local-standin-0000"
  export OPENAI_WEBSOCKET_BASE_URL="ws://127.0.0.1:$MAIN/v1"
  FAST=(--playback-speed 4 --mic-pace 0.005)
  echo "1. voice call: tool, barge-in, two error events ($SHOWN)"
  "$PY" "$EX" --marker "$SHOWN" --user-id e2e-caller --error-case "${FAST[@]}"
  echo "2. same call, FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true ($HIDDEN)"
  FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true "$PY" "$EX" --marker "$HIDDEN" --error-case "${FAST[@]}"
  echo "3. typed question, spoken answer"
  "$PY" "$EX" --typed "What's the weather in Paris?"
  echo "4. text-only session (a trace, not a Voice-tab call)"
  "$PY" "$EX" --text
  echo "5. socket drop mid-response (expect status ERROR)"
  OPENAI_WEBSOCKET_BASE_URL="ws://127.0.0.1:$DROP/v1" "$PY" "$EX" "${FAST[@]}" || true
  echo "6. refused connection (expect connect_failed)"
  OPENAI_API_KEY="sk-bad-standin-0000" "$PY" "$EX" --text || true
else
  echo "LIVE: sessions go to the OpenAI Realtime API and spend credits."
  echo "1. typed question, tool call, spoken answer ($SHOWN)"
  "$PY" "$EX" --typed "What's the weather in Paris?" --marker "$SHOWN" --user-id e2e-caller
  echo "2. same, FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true ($HIDDEN)"
  FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true "$PY" "$EX" --typed "What's the weather in Paris?" --marker "$HIDDEN"
  echo "3. text-only session"
  "$PY" "$EX" --text
  echo "4. refused connection (expect connect_failed)"
  OPENAI_API_KEY="sk-bad-e2e-0000" "$PY" "$EX" --text || true
fi
echo "Done. In Future AGI open project $PROJECT. $SHOWN appears in the traces; $HIDDEN must appear nowhere."
