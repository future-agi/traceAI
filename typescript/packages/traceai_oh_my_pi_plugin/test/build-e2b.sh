#!/bin/bash
set -euo pipefail
cd /work/typescript
pnpm install --ignore-scripts --no-frozen-lockfile
for p in fi-semantic-conventions fi-core agent-core pi-coding-agent pi-plugin oh-my-pi-plugin agents; do pnpm --filter @traceai/$p build; done
pnpm --filter @traceai/agent-core test
pnpm --filter @traceai/oh-my-pi-plugin test
pnpm --filter @traceai/agents test
mkdir -p /work/packs /work/bun /work/host
for p in fi-semantic-conventions fi-core traceai_agent_core traceai_pi_coding_agent traceai_pi_plugin traceai_oh_my_pi_plugin traceai_agents; do
  (cd packages/$p && pnpm pack --pack-destination /work/packs)
done
curl -fsSL https://registry.npmjs.org/@oven/bun-linux-x64/-/bun-linux-x64-1.4.4.tgz | tar -xz -C /work/bun
export PATH=/work/bun/package/bin:$PATH
(cd /work/host && bun add @oh-my-pi/pi-coding-agent@18.17.0)
export PATH=/work/host/node_modules/.bin:$PATH
bun packages/traceai_oh_my_pi_plugin/test/host-compaction.mts
node packages/traceai_oh_my_pi_plugin/test/real-omp.cjs
python3 -I /work/.github/release/release_plan.py --check
