const { build } = require('esbuild');
const { mkdirSync, writeFileSync } = require('node:fs');
mkdirSync('plugin', { recursive: true });
writeFileSync('plugin/plugin.json', JSON.stringify({
  $schema: 'https://antigravity.google/schemas/v1/plugin.json',
  name: 'futureagi-tracing', displayName: 'Future AGI', version: require('./package.json').version,
  description: 'Trace Antigravity CLI turns and tools in Future AGI. Capture is controlled by your local Future AGI configuration.',
  author: {name: 'Future AGI'}, license: 'Apache-2.0'
}, null, 2) + '\n');
build({entryPoints: ['src/hook.ts'], bundle: true, platform: 'node', target: 'node22',
  format: 'cjs', outfile: 'plugin/hook.cjs', sourcemap: false, minify: false}).catch(() => process.exitCode = 1);
