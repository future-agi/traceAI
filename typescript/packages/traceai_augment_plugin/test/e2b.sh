#!/bin/bash
set -euo pipefail
cd /work/typescript
pnpm install --no-frozen-lockfile --ignore-scripts
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/augment-plugin build
pnpm --filter @traceai/agents build
mkdir -p /work/evidence /work/tarballs /work/vendor /work/packed
npm install --prefix /work/vendor --ignore-scripts --no-audit --no-fund @augmentcode/auggie@0.37.0
export PATH="/work/vendor/node_modules/.bin:$PATH"
pnpm --filter @traceai/agent-core test | tee /work/evidence/core-tests.txt
pnpm --filter @traceai/augment-plugin test | tee /work/evidence/augment-tests.txt
pnpm --filter @traceai/agents test | tee /work/evidence/cli-tests.txt
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_augment_plugin traceai_agents; do
  (cd "/work/typescript/packages/$package" && pnpm pack --pack-destination /work/tarballs)
done
npm_config_cache=/work/empty-cache npm install --prefix /work/packed --ignore-scripts --no-audit --no-fund /work/tarballs/*.tgz
node /work/typescript/packages/traceai_augment_plugin/test/real-binary.cjs | tee /work/evidence/real-binary.txt
sha256sum /work/tarballs/*.tgz > /work/evidence/tarball-sha256.txt
