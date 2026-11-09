#!/usr/bin/env bash
set -euo pipefail
cd /work/typescript
# Run only on the staged cloud checkout; the integration owner owns the shared lockfile.
pnpm install --no-frozen-lockfile --ignore-scripts
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/pi-coding-agent build
pnpm --filter @traceai/pi-plugin build
pnpm --filter @traceai/oh-my-pi-plugin build
pnpm --filter @traceai/agents build
pnpm --filter @traceai/agent-core test
pnpm --filter @traceai/pi-coding-agent test --runInBand
pnpm --filter @traceai/pi-plugin test
pnpm --filter @traceai/agents test
mkdir -p /work/packs
for p in fi-semantic-conventions fi-core traceai_agent_core traceai_pi_coding_agent traceai_pi_plugin traceai_oh_my_pi_plugin traceai_agents; do
  (cd packages/$p && pnpm pack --pack-destination /work/packs)
done
node packages/traceai_pi_plugin/test/real-pi.cjs
