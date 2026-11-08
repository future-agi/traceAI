const fs = require('node:fs/promises');
const path = require('node:path');
const { build } = require('esbuild');
const { createVSIX } = require('@vscode/vsce');
(async () => {
  const root = path.resolve(__dirname, '..');
  const pkg = require('../package.json');
  const out = path.join(root, '.vsix-build');
  await fs.rm(out, { recursive: true, force: true });
  await fs.mkdir(out, { recursive: true });
  await build({ entryPoints: [path.join(root, 'src/extension.ts')], outfile: path.join(out, 'extension.js'),
    bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['vscode'], sourcemap: false,
    // jsonc-parser's UMD entry hides relative requires from bundlers. Its ESM entry is fully static.
    alias: { 'jsonc-parser': require.resolve('jsonc-parser/lib/esm/main.js') } });
  await fs.copyFile(path.join(root, 'LICENSE'), path.join(out, 'LICENSE'));
  await fs.writeFile(path.join(out, 'package.json'), JSON.stringify({
    name: 'vscode-extension', publisher: 'future-agi', displayName: 'Future AGI Tracing', version: pkg.version,
    description: pkg.description, license: pkg.license, repository: pkg.repository,
    engines: { vscode: '^1.100.0' }, main: './extension.js', extensionKind: ['ui'], files: ['extension.js', 'LICENSE'],
    categories: ['Other'],
    activationEvents: ['onStartupFinished'],
    // The relay reads only Future AGI's user-level installation record and never workspace files or
    // workspace settings, so Restricted Mode must not silently stop tracing.
    capabilities: { untrustedWorkspaces: { supported: true }, virtualWorkspaces: { supported: true } },
    contributes: { commands: [
      { command: 'futureagi.tracing.status', title: 'Future AGI: Show tracing status' },
      { command: 'futureagi.tracing.toggle', title: 'Future AGI: Disable/Enable tracing' }
    ] }
  }, null, 2));
  await createVSIX({ cwd: out, packagePath: path.join(root, 'extension.vsix'), dependencies: false, skipLicense: false });
  await fs.rm(out, { recursive: true, force: true });
})().catch(() => { console.error('VSIX_BUILD_FAILED'); process.exitCode = 1; });
