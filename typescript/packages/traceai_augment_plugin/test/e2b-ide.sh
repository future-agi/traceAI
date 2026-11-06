#!/bin/bash
set -euo pipefail
cd /work/typescript
mkdir -p /work/evidence /work/tarballs /work/packed
# Lockfile generation happens in the cloud; rerun install frozen before building.
pnpm install --lockfile-only --ignore-scripts --no-frozen-lockfile
pnpm install --frozen-lockfile --ignore-scripts
for package in @traceai/fi-semantic-conventions @traceai/fi-core @traceai/agent-core @traceai/augment-plugin @traceai/agents; do
  pnpm --filter "$package" build
done
npm install --prefix /work/vendor --ignore-scripts --no-audit --no-fund @augmentcode/auggie@0.37.0
export PATH="/work/vendor/node_modules/.bin:$PATH"
pnpm --filter @traceai/agent-core test | tee /work/evidence/core-tests.txt
pnpm --filter @traceai/augment-plugin test | tee /work/evidence/augment-and-ide-tests.txt
pnpm --filter @traceai/agents test | tee /work/evidence/cli-tests.txt
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_augment_plugin traceai_agents; do
  (cd "/work/typescript/packages/$package" && pnpm pack --pack-destination /work/tarballs)
done
npm_config_cache=/work/empty-cache npm install --prefix /work/packed --ignore-scripts --no-audit --no-fund /work/tarballs/*.tgz
FI_PACKED_MODULES=/work/packed/node_modules FI_PACKED_CLI=/work/packed/node_modules/@traceai/agents/dist/cli.js node --test packages/traceai_augment_plugin/test/ide.test.cjs | tee /work/evidence/packed-ide-tests.txt
sha256sum /work/tarballs/*.tgz > /work/evidence/tarball-sha256.txt
node - <<'NODE' > /work/evidence/qualification.json
console.log(JSON.stringify({
  status: 'HOLD', surface: 'augment-ide', realEditorSessions: { vscode: false, intellij: false },
  fixtureOnly: true, modelRequests: 0, noCloudCalls: true,
  blockers: ['AUGMENT_IDE_GUI_QUALIFICATION_REQUIRED', 'AUTHENTICATED_EDITOR_MODEL_SERVICE_REQUIRED', 'REAL_PRODUCT_MEDIA_REQUIRED'],
  scope: 'Package/schema, metadata discovery, shared hook runtime and packed CLI tests only; not real IDE execution.'
}, null, 2));
NODE
