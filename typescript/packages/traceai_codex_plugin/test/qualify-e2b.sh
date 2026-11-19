set -eu
cd /work/typescript
# This lock is staging-only: the integration owner owns the shared lockfile.
pnpm install --ignore-scripts --no-frozen-lockfile --filter @traceai/agents...
npx --yes prettier@3.5.3 --write packages/traceai_codex_plugin/src packages/traceai_codex_plugin/test packages/traceai_codex_plugin/build.cjs packages/traceai_codex_plugin/package.json packages/traceai_codex_plugin/tsconfig.json
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/codex-plugin build
pnpm --filter @traceai/agents build
mkdir -p /work/formatted/codex /work/tarballs /work/consumer
cp -R packages/traceai_codex_plugin/src packages/traceai_codex_plugin/test /work/formatted/codex/
cp packages/traceai_codex_plugin/{build.cjs,package.json,tsconfig.json} /work/formatted/codex/
# Run native qualification even if a shared-core test fails; preserve its exit
# status so the receipt can never report a failing core suite as a green gate.
core_status=0
pnpm --filter @traceai/agent-core test || core_status=$?
pnpm --filter @traceai/codex-plugin test
pnpm --filter @traceai/agents test
for p in fi-semantic-conventions fi-core traceai_agent_core traceai_codex_plugin traceai_agents; do
  (cd packages/$p && pnpm pack --pack-destination /work/tarballs)
done
tar tzf /work/tarballs/traceai-codex-plugin-0.1.0.tgz | grep '.codex-plugin/plugin.json'
tar tzf /work/tarballs/traceai-codex-plugin-0.1.0.tgz | grep 'plugin/hooks/hooks.json'
cd /work/consumer
printf '{"private":true}' > package.json
npm install --ignore-scripts --cache /work/empty-npm-cache /work/tarballs/*.tgz
node --input-type=module -e 'const core = await import("@traceai/agent-core"); if (typeof core.withFileLock !== "function") throw Error("ESM_EXPORT_MISSING")'
npm install -g @openai/codex@0.167.0
export PATH="$(npm prefix -g)/bin:$PATH"
export FI_TEST_PACKAGE_DIR=/work/consumer/node_modules/@traceai/codex-plugin
export FI_TEST_CLI=/work/consumer/node_modules/@traceai/agents/dist/cli.js
unshare -n sh -c 'ip link set lo up; node /work/typescript/packages/traceai_codex_plugin/test/e2b.cjs'
FI_TEST_LONG_TURN=true FI_TEST_EVIDENCE_DIR=/work/evidence-longturn unshare -n sh -c 'ip link set lo up; node /work/typescript/packages/traceai_codex_plugin/test/e2b.cjs'
FI_TEST_NESTED=true FI_TEST_EVIDENCE_DIR=/work/evidence-nested unshare -n sh -c 'ip link set lo up; node /work/typescript/packages/traceai_codex_plugin/test/e2b.cjs'
FI_TEST_NESTED=true FI_TEST_NESTED_REUSE=true FI_TEST_EVIDENCE_DIR=/work/evidence-nested-reuse unshare -n sh -c 'ip link set lo up; node /work/typescript/packages/traceai_codex_plugin/test/e2b.cjs'
# Repeat concurrent parent/child hooks with a fresh home and state.
FI_TEST_EVIDENCE_DIR=/work/evidence-repeat unshare -n sh -c 'ip link set lo up; node /work/typescript/packages/traceai_codex_plugin/test/e2b.cjs'
exit "$core_status"
