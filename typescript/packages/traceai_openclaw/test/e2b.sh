#!/usr/bin/env bash
# Run only inside a fresh E2B sandbox with a slim TypeScript workspace at /work/typescript.
# No Cloud/model credentials. The real-host test uses loopback-only synthetic servers.
set -eu
mkdir -p /work/node /work/host /work/evidence
curl -fsSL https://nodejs.org/dist/v24.16.0/node-v24.16.0-linux-x64.tar.xz | tar -xJ --strip-components=1 -C /work/node
export PATH=/work/node/bin:$PATH
cd /work/typescript
if [ "${FI_TEST_ADAPTER_ONLY:-0}" = 1 ]; then
  # Integration lanes cannot change the coordinator-owned lockfile. Reconcile
  # only this disposable E2B copy, then verify a frozen install of that result.
  pnpm --filter @traceai/agents... install --lockfile-only --no-frozen-lockfile --ignore-scripts
fi
pnpm --filter @traceai/agents... install --frozen-lockfile --ignore-scripts
for package in @traceai/fi-semantic-conventions @traceai/fi-core @traceai/agent-core @traceai/openclaw @traceai/agents; do
  pnpm --filter "$package" build
done
pnpm --filter @traceai/openclaw test
pnpm --filter @traceai/agent-core test
pnpm --filter @traceai/agents test
if [ "${FI_TEST_ADAPTER_ONLY:-0}" != 1 ]; then
  python /work/.github/release/release_plan.py --check
fi
mkdir -p /work/packs /work/consumer
for package in @traceai/fi-semantic-conventions @traceai/fi-core @traceai/agent-core @traceai/openclaw @traceai/agents; do
  pnpm --filter "$package" pack --pack-destination /work/packs
done
cd /work/consumer
printf '{"private":true}\n' > package.json
npm install /work/packs/*.tgz --ignore-scripts --no-audit --no-fund
node -e 'const assert=require("node:assert/strict"); const {openclawAdapter}=require("@traceai/openclaw"); assert.equal(openclawAdapter.id,"openclaw"); import("@traceai/agent-core").then(m=>assert.equal(typeof m.resolveConfig,"function"));'
export FI_TEST_CLI=/work/consumer/node_modules/@traceai/agents/dist/cli.js
node "$FI_TEST_CLI" --help >/work/evidence/packed-help.txt
cd /work/host
npm init -y >/dev/null
npm install openclaw@2026.10.5 --no-audit --no-fund
export PATH=/work/host/node_modules/.bin:$PATH
node /work/typescript/packages/traceai_openclaw/test/real-host.cjs
