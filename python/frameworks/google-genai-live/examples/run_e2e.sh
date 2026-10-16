#!/usr/bin/env bash
# End-to-end run of traceAI-google-genai-live against a real Gemini Live model and a Future AGI
# collector. Needs GEMINI_API_KEY, FI_API_KEY, FI_SECRET_KEY (and FI_BASE_URL for a local
# collector). Each scenario is one short real Live session.
#   usage: [PYTHON=python3.13] examples/run_e2e.sh [project-name] [google-genai version]
# Python 3.10-3.13 (fi-instrumentation-otel declares <3.14).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$(dirname "$HERE")"
PROJECT="${1:-gemini-live-e2e}"
GENAI="${2:-2.31.0}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
"${PYTHON:-python3}" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" -q install "$PKG" "google-genai==$GENAI"
export FI_PROJECT_NAME="$PROJECT"
PY="$WORK/venv/bin/python"
EX="$HERE/voice_tool.py"
MARK="FI-MARKER-$RANDOM$RANDOM"
echo "1. typed question + tool";        "$PY" "$EX"
echo "2. drop + resume (one trace)";    "$PY" "$EX" --resume
echo "3. metadata only ($MARK)";        "$PY" "$EX" --metadata-only --marker "$MARK"
echo "4. setup rejected (expect error)"; "$PY" "$EX" --model gemini-not-a-model-live || true
echo "Done. In Future AGI open project $PROJECT: 4 traces; search for $MARK must return nothing."
