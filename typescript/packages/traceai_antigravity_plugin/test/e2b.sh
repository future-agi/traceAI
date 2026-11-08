set -euo pipefail
cd /work/typescript
# The coordinator owns the repository lock; resolve the new pinned lock dependency
# only in this disposable sandbox, then verify the resulting graph is frozen.
pnpm install --ignore-scripts --no-frozen-lockfile
pnpm install --ignore-scripts --frozen-lockfile
pnpm --filter @traceai/fi-semantic-conventions run build
pnpm --filter @traceai/fi-core run build
pnpm --filter @traceai/agent-core run build
pnpm --filter @traceai/antigravity-plugin run build
pnpm --filter @traceai/agents run build
mkdir -p /work/download /work/agydownload-home
HOME=/work/agydownload-home curl -fsSL https://antigravity.google/cli/install.sh -o /work/download/install.sh
HOME=/work/agydownload-home bash /work/download/install.sh
export PATH="/work/agydownload-home/.local/bin:$PATH"
export AGY_TEST_REAL=1
agy --version
pnpm --filter @traceai/antigravity-plugin run test
unshare --net -- bash -c 'set -eu; ip link set lo up; export AGY_TEST_GENERATION=1 AGY_EVIDENCE_DIR=/work/generation-evidence; node --test /work/typescript/packages/traceai_antigravity_plugin/test/generation.test.cjs /work/typescript/packages/traceai_antigravity_plugin/test/provider-error.test.cjs /work/typescript/packages/traceai_antigravity_plugin/test/native-recovery.test.cjs'
pnpm --filter @traceai/agent-core run test
pnpm --filter @traceai/agents run test
mkdir -p /work/tarballs
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_antigravity_plugin traceai_agents; do
  (cd packages/$package && pnpm pack --pack-destination /work/tarballs)
done
mkdir -p /work/fresh-consumer
cd /work/fresh-consumer
npm install --ignore-scripts --cache /work/fresh-cache /work/tarballs/*.tgz
./node_modules/.bin/fi-agent status antigravity --json > /work/packed-status.json || test "$?" -eq 2
node -e 'const a=require("@traceai/antigravity-plugin"); if(a.PLUGIN_NAME!=="futureagi-tracing")process.exit(1); console.log("PACKED_IMPORT_OK")'
unshare --net -- bash -c 'set -eu; ip link set lo up; export AGY_TEST_GENERATION=1 AGY_EVIDENCE_DIR=/work/packed-generation-evidence AGY_PACKAGE_PATH=/work/fresh-consumer/node_modules/@traceai/antigravity-plugin AGY_CLI_PATH=/work/fresh-consumer/node_modules/@traceai/agents/dist/cli.js; node --test /work/typescript/packages/traceai_antigravity_plugin/test/generation.test.cjs /work/typescript/packages/traceai_antigravity_plugin/test/provider-error.test.cjs /work/typescript/packages/traceai_antigravity_plugin/test/native-recovery.test.cjs'
command -v unshare
