#!/usr/bin/env bash
# Internal cloud-only acceptance entry point. Upload a slim workspace to /work first.
set -euo pipefail
npm install --prefix /work/host @github/copilot@1.0.106
cd /work/typescript
pnpm install --ignore-scripts --frozen-lockfile
for package in fi-semantic-conventions fi-core agent-core copilot agents; do
  pnpm --filter "@traceai/$package" build
done
for package in agent-core agents copilot; do
  pnpm --filter "@traceai/$package" test
done
python3 -I /work/typescript/packages/traceai_agents/test/credential-pty.py
mkdir -p /work/packs
for folder in fi-semantic-conventions fi-core traceai_agent_core traceai_copilot traceai_agents; do
  (cd "/work/typescript/packages/$folder" && pnpm pack --pack-destination /work/packs)
done
npm install --prefix /work/consumer --ignore-scripts /work/packs/*.tgz
mkdir -p /work/evidence
sha256sum /work/packs/*.tgz > /work/evidence/packages.sha256
# Egress is blocked only after registry downloads. Local model and OTLP endpoints remain reachable.
iptables -I OUTPUT 1 -o lo -j ACCEPT
iptables -I OUTPUT 2 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -j REJECT
trap 'iptables -F OUTPUT' EXIT
AGENTS_TEST_CLI=/work/consumer/node_modules/@traceai/agents/dist/cli.js pnpm --filter @traceai/copilot test:binary
