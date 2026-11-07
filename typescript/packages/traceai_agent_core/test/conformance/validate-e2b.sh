#!/usr/bin/env bash
set -uo pipefail
mkdir -p /opt/Cursor/resources/app
printf '{"version":"3.24.18"}' > /opt/Cursor/resources/app/package.json
npm install --prefix /work/conformance-tools --ignore-scripts --no-audit --no-fund prettier@3.4.2 > /work/conformance-format-install.log 2>&1 || exit 10
node /work/conformance-tools/node_modules/prettier/bin/prettier.cjs --write /work/typescript/packages/traceai_agent_core/test/conformance/*.cjs /work/typescript/packages/traceai_agent_core/test/conformance/*.ts /work/typescript/packages/traceai_agent_core/test/conformance/drivers/*.cjs /work/typescript/packages/traceai_agent_core/test/conformance.test.cjs > /work/conformance-format.log 2>&1 || { cat /work/conformance-format.log; exit 11; }
bash /work/typescript/packages/traceai_agent_core/test/conformance/validate.sh
suite=$?
cd /work/typescript
pnpm --filter @traceai/agent-core test > /work/conformance-core-tests.log 2>&1
core=$?
pnpm exec tsc --noEmit --skipLibCheck --module node16 --moduleResolution node16 --target es2022 packages/traceai_agent_core/test/conformance/type-consumer.ts > /work/conformance-types.log 2>&1
types=$?
mkdir -p /work/conformance-pack
pnpm --dir packages/traceai_agent_core pack --pack-destination /work/conformance-pack > /work/conformance-pack.log 2>&1
pack=$?
printf '{"suite":%s,"coreTests":%s,"typeConsumer":%s,"packagePack":%s}\n' "$suite" "$core" "$types" "$pack" > /work/conformance-validation.json
cat /work/conformance-validation.json
exit "$suite"
