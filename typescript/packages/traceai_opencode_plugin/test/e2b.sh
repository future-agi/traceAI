#!/usr/bin/env bash
set -euo pipefail
cd /work
pnpm install --ignore-scripts --frozen-lockfile
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/agents build
pnpm --filter @traceai/opencode-plugin build
# The slim stage places TypeScript at /work rather than /work/typescript.
cp -R /work/integrations /integrations
pnpm --filter @traceai/agent-core test
pnpm --filter @traceai/opencode-plugin test
pnpm --filter @traceai/agents test
mkdir -p /work/packs
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_agents traceai_opencode_plugin; do
  (cd "/work/packages/$package" && pnpm pack --out /work/packs/"$package".tgz)
  tar -xOf /work/packs/"$package".tgz package/package.json > /work/packs/"$package".tgz.json
done
npm install --prefix /work/host @opencode/cli@2.2.5 @opencode/plugin@2.2.5 @opencode/client@2.2.5
if ! command -v iptables >/dev/null; then apt-get update -qq && apt-get install -y -qq iptables; fi
node /work/packages/traceai_opencode_plugin/test/e2b.mjs
