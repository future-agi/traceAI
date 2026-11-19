#!/usr/bin/env bash
set -euo pipefail
cd /work/typescript
pnpm install --no-frozen-lockfile --ignore-scripts
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/muse-plugin build
pnpm --filter @traceai/agents build
pnpm --filter @traceai/muse-plugin test
mkdir -p /work/evidence /work/tarballs
for package in @traceai/fi-semantic-conventions @traceai/fi-core @traceai/agent-core @traceai/muse-plugin @traceai/agents; do
  pnpm --filter "$package" pack --pack-destination /work/tarballs
done
mkdir -p /work/installed
npm install --prefix /work/installed --ignore-scripts --no-audit --no-fund /work/tarballs/*.tgz
curl --retry 2 --connect-timeout 30 --max-time 240 -fsSL 'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=1.4.14-R7309.1&file=muse-x86-linux' -o /usr/local/bin/muse
printf '%s  %s\n' '0809aac7cb58d18f5022b6d05e02aa1974683c0ec430e5f04d25b60ed17de2da' '/usr/local/bin/muse' | sha256sum -c -
chmod 755 /usr/local/bin/muse
if ! command -v ip >/dev/null; then apt-get update -qq && apt-get install -y -qq iproute2; fi
useradd -m -s /bin/bash musefixture
chown -R musefixture:musefixture /work/evidence
# A fresh network namespace has only loopback and no route to model/Cloud/vendor endpoints.
# E2B control remains outside this child namespace. Do not fall back to unrestricted execution.
unshare --net bash -c 'set -e; ip link set lo up; ip route > /work/evidence/routes.txt; runuser -u musefixture -- node /work/typescript/packages/traceai_muse_plugin/test/native.cjs'
