#!/usr/bin/env bash
# End-to-end run of the Arconia example.
#   1. unit/integration tests (stand-in collector, no keys needed)
#   2. the packaged app, configured only through application.yaml + FI_* env, sends traces to a real Future AGI
#   3. precedence check: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT beats the yaml
#
# Needs JDK 21 and Maven (JAVA_HOME respected). For step 2: FI_API_KEY, FI_SECRET_KEY and optionally
#   FI_BASE_URL (default https://api.futureagi.com), FI_PROJECT_NAME (default arconia-example).
# Set ARCONIA_VERSION to run another Arconia BOM (default: the one in pom.xml).
set -u
cd "$(dirname "$0")"
PORT="${PORT:-18269}"
MVN_ARGS=()
[ -n "${ARCONIA_VERSION:-}" ] && MVN_ARGS+=("-Darconia.version=$ARCONIA_VERSION")

echo "== 1. tests"
mvn -q "${MVN_ARGS[@]}" test || { echo "TESTS FAILED"; exit 1; }
mvn -q "${MVN_ARGS[@]}" -DskipTests package || exit 1
JAR=target/arconia-example-1.0.0.jar

if [ -z "${FI_API_KEY:-}" ] || [ -z "${FI_SECRET_KEY:-}" ]; then
  echo "FI_API_KEY / FI_SECRET_KEY not set: skipping the live steps."; exit 0
fi
export FI_PROJECT_NAME="${FI_PROJECT_NAME:-arconia-example}"
MARKER="${MARKER:-ARCONIA$(date +%s)}"

run_app() {  # run_app <log> ; makes 2 requests, then SIGTERM (context close flushes the batch)
  SERVER_PORT=$PORT java -jar "$JAR" > "$1" 2>&1 &
  local pid=$!
  for _ in $(seq 40); do curl -s "localhost:$PORT/hello" > /dev/null && break; sleep 0.5; done
  curl -s "localhost:$PORT/hello?name=$MARKER" > /dev/null
  curl -s -o /dev/null "localhost:$PORT/fail?reason=$MARKER"
  kill -TERM "$pid"; wait "$pid" 2> /dev/null
}

echo "== 2. live export to ${FI_BASE_URL:-https://api.futureagi.com} project=$FI_PROJECT_NAME marker=$MARKER"
run_app /tmp/arconia-example-live.log
echo "sent. Check the project in Future AGI (spans: GET /hello x2, GET /fail with ERROR status)."

echo "== 3. env var beats yaml"
python3 - "$PORT" <<'PY' &
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(s):
        s.rfile.read(int(s.headers.get('content-length', 0)))
        open('/tmp/arconia-env-hits', 'a').write(s.path + '\n')
        s.send_response(200); s.send_header('content-length', '0'); s.end_headers()
    def log_message(*a): pass
http.server.HTTPServer(('127.0.0.1', 18999), H).serve_forever()
PY
STUB=$!; rm -f /tmp/arconia-env-hits; sleep 1
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:18999/v1/traces FI_BASE_URL=http://127.0.0.1:1 run_app /tmp/arconia-example-env.log
kill "$STUB" 2> /dev/null
if [ -s /tmp/arconia-env-hits ]; then echo "PASS env var won: $(sort -u /tmp/arconia-env-hits | tr '\n' ' ')"; else echo "FAIL env var did not win"; exit 1; fi
