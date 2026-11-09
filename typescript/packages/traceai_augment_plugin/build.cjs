const { execFileSync } = require('node:child_process');
const { mkdirSync } = require('node:fs');
execFileSync(process.execPath, [require.resolve('typescript/bin/tsc'), '-p', 'tsconfig.json'], { stdio: 'inherit' });
mkdirSync('runtime', { recursive: true });
require('esbuild').buildSync({
  entryPoints: ['src/hook.ts'], outfile: 'runtime/hook.cjs', bundle: true,
  platform: 'node', target: 'node22', format: 'cjs', mainFields: ['module', 'main'],
  legalComments: 'eof', sourcemap: false,
});
