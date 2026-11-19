#!/usr/bin/env bash
# Cloud-only clean tarball smoke test. Fixture tests are not real Kiro IDE runs.
set -euo pipefail
mkdir -p /work/packs /work/evidence
for pkg in fi-semantic-conventions fi-core traceai_agent_core traceai_kiro_plugin traceai_agents; do
  (cd "/work/typescript/packages/$pkg" && pnpm pack --pack-destination /work/packs)
done
CONSUMER=$(mktemp -d /tmp/kiro-consumer.XXXXXX)
mkdir -p "$CONSUMER/home" "$CONSUMER/cache"
printf '%s\n' '{"name":"kiro-clean-tarball-smoke","version":"0.0.0","private":true}' > "$CONSUMER/package.json"
HOME="$CONSUMER/home" npm --prefix "$CONSUMER" --cache "$CONSUMER/cache" install --ignore-scripts --package-lock=false /work/packs/*.tgz
node - "$CONSUMER" <<'JS' > /work/evidence/ide-pack.json
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const root = process.argv[2];
const plugin = path.join(root, 'node_modules/@traceai/kiro-plugin');
const ide = require(path.join(plugin, 'dist/ide/index.js'));
assert.equal(ide.IDE_QUALIFIED, false);
assert(fs.existsSync(path.join(plugin, 'dist/ide-hook.cjs')));
const command = [path.join(root, 'node_modules/@traceai/agents/dist/cli.js'), 'setup', 'kiro-ide', '--project', 'coding-agents', '--json'];
const result = spawnSync(process.execPath, command, { cwd: root, env: { HOME: path.join(root, 'home'), FI_AGENT_CONFIG_DIR: path.join(root, 'protected') }, encoding: 'utf8', timeout: 5000 });
assert.equal(result.status, 2);
const status = JSON.parse(result.stdout);
assert.equal(status.code, 'KIRO_IDE_GUI_AND_PAYLOAD_QUALIFICATION_REQUIRED');
assert.equal(result.stderr, '');
assert.equal(fs.existsSync(path.join(root, 'protected')), false);
console.log(JSON.stringify({ cleanTarballInstall: true, filesPresent: true, setupGate: status.code, realIdeTraceEvidence: false }));
JS
