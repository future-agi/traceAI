#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/evidence /work/tarballs /work/vendor
cd /work/typescript
pnpm install --lockfile-only --ignore-scripts --no-frozen-lockfile
pnpm install --frozen-lockfile --ignore-scripts
for p in fi-semantic-conventions fi-core agent-core muse-plugin gemini-cli kiro-plugin augment-plugin devin-cascade agents; do pnpm --filter @traceai/$p build; done
npm install --prefix /work/vendor --ignore-scripts --no-audit --no-fund @augmentcode/auggie@0.37.0 @google/gemini-cli@0.67.0
export PATH="/work/vendor/node_modules/.bin:$PATH"
status=0
for p in agent-core muse-plugin gemini-cli kiro-plugin augment-plugin devin-cascade agents; do
  pnpm --filter @traceai/$p test > /work/evidence/$p-tests.txt 2>&1 || status=1
  tail -12 /work/evidence/$p-tests.txt
done
for p in fi-semantic-conventions fi-core agent-core muse-plugin gemini-cli kiro-plugin augment-plugin devin-cascade agents; do pnpm --filter @traceai/$p pack --pack-destination /work/tarballs; done
mkdir /work/packed
printf '{"private":true}' > /work/packed/package.json
npm_config_cache=/work/empty-cache npm install --prefix /work/packed --ignore-scripts --no-audit --no-fund /work/tarballs/*.tgz
ln -s /work/packed /work/installed
sha256sum /work/tarballs/*.tgz > /work/evidence/tarball-sha256.txt
# All actual host execution is isolated from vendor/Cloud/model egress.
if ! command -v ip >/dev/null; then apt-get update -qq && apt-get install -y -qq iproute2; fi
run_case() {
  local name="$1"; shift
  if "$@" > "/work/evidence/$name.log" 2>&1; then echo "$name PASS"; else echo "$name FAIL"; tail -40 "/work/evidence/$name.log"; status=1; fi
}
run_case gemini-native unshare --net bash -c 'ip link set lo up; node /work/typescript/packages/traceai_gemini_cli/test/real-binary.cjs /work/packed /work/vendor/node_modules/.bin/gemini /work/evidence/gemini'
run_case augment-native unshare --net bash -c 'ip link set lo up; node /work/typescript/packages/traceai_augment_plugin/test/real-binary.cjs'
run_case cascade-packed unshare --net bash -c 'ip link set lo up; node /work/typescript/packages/traceai_devin_cascade/test/packed-smoke.cjs /work/packed /work/typescript/packages/traceai_devin_cascade/test/fixtures/documented-transcript.jsonl'
# Pinned native distributions are downloaded before execution, into fresh directories.
mkdir /work/muse-download
curl --retry 2 --connect-timeout 30 --max-time 240 -fsSL 'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=1.4.14-R7309.1&file=muse-x86-linux' -o /work/muse-download/muse
printf '%s  %s\n' '0809aac7cb58d18f5022b6d05e02aa1974683c0ec430e5f04d25b60ed17de2da' /work/muse-download/muse | sha256sum -c -
chmod 755 /work/muse-download/muse
ln -s /work/muse-download/muse /usr/local/bin/muse
useradd -m -s /bin/bash musefixture
chown -R musefixture:musefixture /work/evidence
run_case muse-native unshare --net bash -c 'ip link set lo up; runuser -u musefixture -- node /work/typescript/packages/traceai_muse_plugin/test/native.cjs'
mkdir /work/kiro-download /work/kiro-extract
curl --retry 2 --max-time 240 -fsSL https://prod.download.cli.kiro.dev/stable/3.0.0/kirocli-x86_64-linux.zip -o /work/kiro-download/kiro.zip
sha256sum /work/kiro-download/kiro.zip > /work/evidence/kiro-sha256.txt
unzip -q /work/kiro-download/kiro.zip -d /work/kiro-extract
export KIRO_TEST_BIN=$(find /work/kiro-extract -type f -name kiro-cli | head -1)
chmod +x "$KIRO_TEST_BIN"
export PATH="$(dirname "$KIRO_TEST_BIN"):$PATH"
export KIRO_TEST_CLI=/work/packed/node_modules/@traceai/agents/dist/cli.js
run_case kiro-native unshare --net bash -c 'ip link set lo up; node /work/typescript/packages/traceai_kiro_plugin/test/real-kiro.cjs'
run_case kiro-ide-probe bash /work/typescript/packages/traceai_kiro_plugin/test/ide-probe.sh
FI_PACKED_MODULES=/work/packed/node_modules FI_PACKED_CLI=/work/packed/node_modules/@traceai/agents/dist/cli.js node --test /work/typescript/packages/traceai_augment_plugin/test/ide.test.cjs > /work/evidence/augment-ide-packed.txt 2>&1 || status=1
cp /work/packed/capture.json /work/evidence/cascade-packed.json || true
exit "$status"
