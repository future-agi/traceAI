#!/usr/bin/env bash
# Run the forwarder end to end against a Future AGI collector, with ElevenLabs stood in.
#   FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=http://localhost:14338 ./run_e2e.sh <project> [out-dir]
# Needs the package installed in the current Python (pip install -e .. or pip install traceAI-elevenlabs-agents).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
project="${1:?usage: run_e2e.sh <project> [out-dir]}"
out="${2:-$here/.e2e}"
: "${FI_API_KEY:?FI_API_KEY is not set}"
: "${FI_SECRET_KEY:?FI_SECRET_KEY is not set}"
mkdir -p "$out"
python "$here/run_e2e.py" --project "$project" --out "$out/e2e-report.json"
