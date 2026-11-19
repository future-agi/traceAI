#!/usr/bin/env bash
# E2B-only validation; dependency preparation has internet, agent execution has only loopback.
set -euo pipefail
cd /work/typescript
pnpm --filter @traceai/agents... install --frozen-lockfile --ignore-scripts
pnpm --filter @traceai/fi-semantic-conventions --filter @traceai/fi-core --filter @traceai/agent-core --filter @traceai/claude-code --filter @traceai/agents run build
pnpm --filter @traceai/agent-core --filter @traceai/claude-code --filter @traceai/agents test
mkdir -p /work/host /work/packs /work/clean /work/evidence
npm install --prefix /work/host --no-audit --no-fund @anthropic-ai/claude-code@2.1.318
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_claude_code traceai_agents; do
  pnpm --dir "packages/$package" pack --pack-destination /work/packs
done
npm install --prefix /work/clean --cache /work/empty-npm-cache --ignore-scripts --no-audit --no-fund /work/packs/*.tgz
export CLAUDE_ADAPTER_ROOT=/work/clean/node_modules/@traceai/claude-code
export CLAUDE_AGENT_CLI=/work/clean/node_modules/@traceai/agents/dist/cli.js
unshare --net bash -c 'ip link set lo up; node /work/typescript/packages/traceai_claude_code/test/binary.cjs'
