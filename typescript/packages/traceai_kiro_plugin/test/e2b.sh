#!/usr/bin/env bash
set -euo pipefail
cd /work/typescript
pnpm install --no-frozen-lockfile --ignore-scripts
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/kiro-plugin build
pnpm --filter @traceai/agents build
pnpm --filter @traceai/agent-core test
mkdir -p /work/evidence
export KIRO_EVIDENCE_DIR=/work/evidence
pnpm --filter @traceai/kiro-plugin test
pnpm --filter @traceai/agents test
mkdir -p /work/evidence /work/tarballs
cd /work/typescript/packages/traceai_kiro_plugin
pnpm pack --out /work/tarballs/kiro-plugin.tgz
# Packaged bundle must run without node_modules or build-tree references.
mkdir /work/clean-package
cd /work/clean-package
tar -xzf /work/tarballs/kiro-plugin.tgz
printf '{}\n' | env -i PATH=/bin HOME=/work/clean-package "$(command -v node)" /work/clean-package/package/dist/hook.cjs Stop /work/clean-config /work/clean-agent
for pkg in fi-semantic-conventions fi-core traceai_agent_core traceai_agents; do
  cd "/work/typescript/packages/$pkg"
  pnpm pack --out "/work/tarballs/$pkg.tgz"
done
mkdir /work/clean-consumer
cd /work/clean-consumer
printf '{"private":true}' > package.json
npm install --ignore-scripts --no-audit --no-fund /work/tarballs/*.tgz
export KIRO_TEST_CLI=/work/clean-consumer/node_modules/@traceai/agents/dist/cli.js
cd /work
bash /work/typescript/packages/traceai_kiro_plugin/test/real-kiro.sh
