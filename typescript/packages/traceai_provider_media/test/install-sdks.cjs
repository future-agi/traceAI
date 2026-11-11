/* Run only on an isolated qualification worker, never in the SDK or application. */
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[2];
if (!root || !path.isAbsolute(root)) throw Error('Supply an absolute isolated SDK directory');
fs.mkdirSync(root, { recursive: true });
const entries = [
  ['openai', 'openai', '4.0.0', '7.55.0'], ['groq', 'groq-sdk', '0.3.0', '1.6.0'],
  ['genai', '@google/genai', '1.33.0', '3.1.0'], ['gemini', '@google/generative-ai', '0.1.0', '0.24.1'],
  ['vertex', '@google-cloud/vertexai', '1.0.0', '1.12.0'], ['bedrock', '@aws-sdk/client-bedrock-runtime', '3.693.0', '3.1166.0'],
  // Typed Mistral image chunks start at 1.1.0; 0.x and 1.0.0 are legacy text clients and are not advertised.
  ['mistral', '@mistralai/mistralai', '1.1.0', '3.5.0'], ['portkey', 'portkey-ai', '1.0.0', '3.1.0'],
];
const dependencies = {}, versions = {};
// Pinned latest releases keep CI reproducible. MEDIA_SDK_LATEST=resolve re-queries npm when re-qualifying.
const resolve = process.env.MEDIA_SDK_LATEST === 'resolve';
for (const [name, pkg, floor, pinned] of entries) {
  const latest = resolve || !pinned ? execFileSync('npm', ['view', pkg, 'version'], { encoding: 'utf8' }).trim() : pinned;
  versions[name] = { package: pkg, floor, latest };
  dependencies[`sdk-${name}-floor`] = `npm:${pkg}@${floor}`;
  dependencies[`sdk-${name}-latest`] = `npm:${pkg}@${latest}`;
}
fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ private: true, dependencies }, null, 2));
fs.writeFileSync(path.join(root, 'versions.json'), JSON.stringify(versions, null, 2));
console.log(JSON.stringify(versions, null, 2));
execFileSync('npm', ['install', '--prefix', root, '--ignore-scripts', '--no-audit', '--no-fund', '--legacy-peer-deps'], { stdio: 'inherit' });
