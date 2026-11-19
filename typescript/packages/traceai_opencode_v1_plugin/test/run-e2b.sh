#!/usr/bin/env bash
# Run inside an E2B toolchain sandbox with the slim TypeScript workspace staged.
set -euo pipefail
cd /work/typescript
pnpm install --frozen-lockfile --ignore-scripts
for package in @traceai/fi-semantic-conventions @traceai/fi-core @traceai/agent-core @traceai/agents @traceai/opencode-v1-plugin; do
  pnpm --filter "$package" build
done
for package in @traceai/agent-core @traceai/agents @traceai/opencode-v1-plugin; do
  pnpm --filter "$package" test
done
mkdir -p /work/packs
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_opencode_v1_plugin; do
  (cd "packages/$package" && pnpm pack --pack-destination /work/packs)
done
npm install --prefix /work/host opencode-ai@1.19.0
node packages/traceai_opencode_v1_plugin/test/isolated-e2b.mjs
