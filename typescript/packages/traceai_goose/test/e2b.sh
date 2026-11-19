#!/usr/bin/env bash
set -euo pipefail
cd /work/typescript
pnpm install --frozen-lockfile --ignore-scripts
pnpm --filter @traceai/agents... run build
pnpm --filter @traceai/goose test
pnpm --filter @traceai/agent-core test
pnpm --filter @traceai/agents test
mkdir -p /work/packs /work/consumer /work/vendor
for p in fi-semantic-conventions fi-core traceai_agent_core traceai_goose traceai_agents; do
  pnpm --dir /work/typescript/packages/$p pack --pack-destination /work/packs
done
npm --prefix /work/consumer install --ignore-scripts /work/packs/*.tgz
curl -fsSL --retry 2 https://github.com/aaif-goose/goose/releases/download/v1.54.0/goose-x86_64-unknown-linux-gnu.tar.gz -o /work/vendor/goose.tar.gz
echo 'ab396c95aebbd28b4d38c175234be8d4a3ea0c96e7ccb24c307e4618c9be57e1  /work/vendor/goose.tar.gz' | sha256sum -c -
tar -xzf /work/vendor/goose.tar.gz -C /work/vendor
# All package and agent downloads finish before execution. No paid provider or Cloud route exists.
unshare --net bash -c 'ip link set lo up; python3 -I /work/typescript/packages/traceai_goose/test/e2e.py'
