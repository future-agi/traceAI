#!/usr/bin/env bash
# End-to-end run of the Quarkus LangChain4j example.
#   1. tests against a scripted model server and a stand-in OTLP collector (no keys needed)
#   2. live: the packaged app, configured only through application.properties + environment, calls a real
#      OpenAI-compatible model and exports to a real Future AGI:
#        a) one question that makes a tool call          -> project $FI_PROJECT_NAME
#        b) one failing model call (unknown model)        -> project $FI_PROJECT_NAME
#        c) the same two with the content opt-ins on      -> project $FI_PROJECT_NAME-optin
#   3. flush: a batch-processor build (quarkus.otel.simple=false) exits normally and its spans still arrive
#                                                         -> project $FI_PROJECT_NAME-batch
#   4. negative: built without quarkus-opentelemetry, nothing is exported (local stub collector)
#
# Needs JDK 17+ (21 tested) and Maven; JAVA_HOME is respected.
# Live steps need: FI_API_KEY, FI_SECRET_KEY, OPENAI_API_KEY and optionally FI_BASE_URL (default
#   https://api.futureagi.com), FI_PROJECT_NAME (default quarkus-langchain4j-example), OPENAI_BASE_URL, OPENAI_MODEL,
#   OPENAI_FAIL_MODEL (a model name the endpoint rejects, default no-such-model-quarkus-example), MARKER.
# QLC4J_VERSION + QUARKUS_VERSION select another tested pair, e.g. 1.8.2 + 3.27.2 (tests are skipped on 1.8.x).
set -u
cd "$(dirname "$0")"

MVN_ARGS=(-B -q)
if [ -n "${QLC4J_VERSION:-}" ]; then
  MVN_ARGS+=("-Dquarkus-langchain4j.version=$QLC4J_VERSION" "-Dquarkus.platform.version=${QUARKUS_VERSION:?set QUARKUS_VERSION too}")
  case "$QLC4J_VERSION" in 1.8.*|1.9.*|1.10.*|1.11.*) MVN_ARGS+=(-Dpre112) ;; esac
  case "$QUARKUS_VERSION" in 3.2[0-9].*|3.1[0-9].*)
    # OpenTelemetry SDK < 1.51 refuses quarkus.otel.span.event.count.limit=0 at startup.
    export QUARKUS_OTEL_SPAN_EVENT_COUNT_LIMIT=128
    echo "note: Quarkus $QUARKUS_VERSION: span events back on, exception messages and stack traces are exported" ;;
  esac
fi
RUN() { java -jar "$1/quarkus-run.jar" "${@:2}"; }
expect_answer() { RUN "$@" || { echo "FAIL: the AI service call did not succeed"; exit 1; }; }
expect_model_failure() {
  local out; out=$(RUN "$@" 2>&1)
  if echo "$out" | grep -q "The AI service call failed"; then echo "model call failed as expected"; else
    echo "FAIL: the app did not reach the model call"; echo "$out" | grep -E "ERROR|Caused by" | head -5; exit 1; fi
}

echo "== 1. tests"
mvn "${MVN_ARGS[@]}" clean test || { echo "TESTS FAILED"; exit 1; }

echo "== build: default app, batch-processor app, app without quarkus-opentelemetry"
mvn "${MVN_ARGS[@]}" -DskipTests package || exit 1
mvn "${MVN_ARGS[@]}" -DskipTests package -Dquarkus.otel.simple=false -Dquarkus.package.output-directory=batch-app || exit 1
mvn "${MVN_ARGS[@]}" -DskipTests package -DnoOtel -Dquarkus.package.output-directory=no-otel-app || exit 1

echo "== 4. negative: no quarkus-opentelemetry, local stub collector"
STUB_PORT="${STUB_PORT:-18270}"
rm -f /tmp/quarkus-lc4j-stub-hits
python3 - "$STUB_PORT" <<'PY' &
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(s):
        s.rfile.read(int(s.headers.get('content-length', 0)))
        open('/tmp/quarkus-lc4j-stub-hits', 'a').write(s.path + '\n')
        s.send_response(200); s.send_header('content-length', '0'); s.end_headers()
    def log_message(*a): pass
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
PY
STUB=$!
sleep 1
# The model call fails fast (closed port): only the export path matters here.
FI_BASE_URL="http://127.0.0.1:$STUB_PORT" FI_API_KEY=k FI_SECRET_KEY=s FI_PROJECT_NAME=negative \
  OPENAI_API_KEY=k OPENAI_BASE_URL=http://127.0.0.1:1/v1/ RUN target/no-otel-app "What is the weather in Paris?" > /tmp/quarkus-lc4j-negative.log 2>&1
sleep 2; kill "$STUB" 2> /dev/null
if [ -s /tmp/quarkus-lc4j-stub-hits ]; then echo "FAIL exported without quarkus-opentelemetry"; exit 1; fi
echo "PASS nothing exported without quarkus-opentelemetry"

if [ -z "${FI_API_KEY:-}" ] || [ -z "${FI_SECRET_KEY:-}" ] || [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "FI_API_KEY / FI_SECRET_KEY / OPENAI_API_KEY not set: skipping the live steps."; exit 0
fi
export FI_PROJECT_NAME="${FI_PROJECT_NAME:-quarkus-langchain4j-example}"
MARKER="${MARKER:-QLC4J$(date +%s)}"
FAIL_MODEL="${OPENAI_FAIL_MODEL:-no-such-model-quarkus-example}"
BASE="$FI_PROJECT_NAME"

echo "== 2a. live tool call -> $BASE (marker $MARKER)"
expect_answer target/quarkus-app "What is the weather in Paris$MARKER right now?"
echo "== 2b. live failing model call -> $BASE"
OPENAI_MODEL="$FAIL_MODEL" expect_model_failure target/quarkus-app "What is the weather in Oslo$MARKER right now?"

echo "== 2c. content opt-in -> $BASE-optin"
export QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_PROMPT=true QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_COMPLETION=true \
  QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_TOOL_ARGUMENTS=true QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_TOOL_RESULT=true \
  FI_EXPORT_ERROR_DETAILS=true
FI_PROJECT_NAME="$BASE-optin" expect_answer target/quarkus-app "What is the weather in Rome$MARKER right now?"
FI_PROJECT_NAME="$BASE-optin" OPENAI_MODEL="$FAIL_MODEL" expect_model_failure target/quarkus-app "What is the weather in Bern$MARKER right now?"
unset QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_PROMPT QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_COMPLETION \
  QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_TOOL_ARGUMENTS QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_TOOL_RESULT FI_EXPORT_ERROR_DETAILS

echo "== 3. batch processor, normal exit -> $BASE-batch"
FI_PROJECT_NAME="$BASE-batch" expect_answer target/batch-app "What is the weather in Madrid$MARKER right now?"

echo "sent. Check the projects in Future AGI: $BASE (tool-call trace + ERROR trace), $BASE-optin, $BASE-batch."
echo "marker: $MARKER"
