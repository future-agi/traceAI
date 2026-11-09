#!/usr/bin/env bash
# Run only inside an E2B sandbox, after staging the repository without build products.
set -uo pipefail
cd /work/typescript
pnpm install --frozen-lockfile --ignore-scripts > /work/conformance-install.log 2>&1 || { tail -30 /work/conformance-install.log; exit 3; }
pnpm --workspace-concurrency=2 --filter '@traceai/agents...' --filter '@traceai/cursor-plugin...' --filter '@traceai/opencode-v1-plugin...' --filter '@traceai/opencode-plugin...' --filter '@traceai/pi-coding-agent...' run build > /work/conformance-build.log 2>&1 || { tail -50 /work/conformance-build.log; exit 4; }
python3 -m venv /work/conformance-venv
/work/conformance-venv/bin/pip install 'opentelemetry-sdk==1.38.0' 'opentelemetry-exporter-otlp-proto-http==1.38.0' /work/python/frameworks/hermes-agent > /work/conformance-python.log 2>&1 || { tail -30 /work/conformance-python.log; exit 5; }
node --test packages/traceai_agent_core/test/conformance.test.cjs > /work/conformance-selftest.log 2>&1 || { cat /work/conformance-selftest.log; exit 6; }
CONFORMANCE_EVIDENCE=/work/conformance-results/wire node packages/traceai_agent_core/test/conformance/run.cjs > /work/conformance.log 2>&1
rc=$?
cat /work/conformance.log
echo "CONFORMANCE_RC=$rc"
exit "$rc"
