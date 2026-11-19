#!/usr/bin/env bash
# Run only in the cloud sandbox. This does not qualify an editor prompt or GUI session.
set -euo pipefail
cd /work/typescript
mkdir -p /work/evidence /work/packed
pnpm --filter @traceai/copilot... --filter @traceai/agents... install --ignore-scripts --frozen-lockfile
pnpm --filter @traceai/fi-semantic-conventions build
pnpm --filter @traceai/fi-core build
pnpm --filter @traceai/agent-core build
pnpm --filter @traceai/copilot build
pnpm --filter @traceai/agents build
pnpm --filter @traceai/agent-core test
pnpm --filter @traceai/copilot test
pnpm --filter @traceai/agents test
for directory in fi-semantic-conventions fi-core traceai_agent_core traceai_copilot traceai_agents; do
  (cd "packages/$directory" && pnpm pack --pack-destination /work/packed)
done
mkdir -p /work/clean-install
cd /work/clean-install
printf '{"name":"isolated-editor-consumer","private":true,"version":"1.0.0"}\n' > package.json
npm install --ignore-scripts --cache /work/empty-npm-cache /work/packed/*.tgz
node -e 'const assert=require("node:assert/strict");const a=require("@traceai/copilot/vscode");assert.equal(a.vscodeAdapter.id,"copilot-vscode");assert.deepEqual(a.qualifiedEditorVersions,[])'
node --input-type=module -e 'import assert from "node:assert/strict";import {vscodeAdapter} from "@traceai/copilot/vscode";assert.equal(vscodeAdapter.id,"copilot-vscode")'
set +e
HOME=/work/clean-home FI_AGENT_CONFIG_DIR=/work/clean-config ./node_modules/.bin/fi-agent setup copilot-vscode --json > /work/evidence/clean-setup.json
result=$?
set -e
[ "$result" = 2 ]
[ ! -e /work/clean-config/credentials.json ]
node -e 'const a=require("node:assert/strict");a.equal(require("/work/evidence/clean-setup.json").code,"VSCODE_GUI_AND_TRANSPORT_QUALIFICATION_REQUIRED")'
# Electron's executable needs its system libraries even for --version. Install only in E2B.
apt-get update -qq
sound=libasound2
if apt-cache show libasound2t64 >/dev/null 2>&1; then sound=libasound2t64; fi
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libnspr4 libnss3 libgtk-3-0 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 "$sound"
# Fetch the pinned distribution before the metadata probe. No account or model request occurs.
download=$(mktemp -d /tmp/vscode-download.XXXXXXXX)
extracted=$(mktemp -d /tmp/vscode-extract.XXXXXXXX)
url=$(node -p 'require("/work/typescript/packages/traceai_copilot/test/vscode-qualification.json").binaryProbe.url')
curl --fail --location --silent --show-error "$url" -o "$download/vscode.tar.gz"
expected=$(node -p 'require("/work/typescript/packages/traceai_copilot/test/vscode-qualification.json").binaryProbe.sha256')
printf '%s  %s\n' "$expected" "$download/vscode.tar.gz" | sha256sum -c -
tar -xzf "$download/vscode.tar.gz" -C "$extracted"
node /work/typescript/packages/traceai_copilot/test/vscode-binary-probe.cjs "$download/vscode.tar.gz" "$extracted" /work/evidence
printf 'All scoped package checks passed; real editor tracing remains HOLD.\n'
