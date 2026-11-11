/* Run only on an isolated qualification worker, never in the SDK or application. */
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[2];
if (!root || !path.isAbsolute(root)) throw Error('Supply an absolute isolated SDK directory');
fs.mkdirSync(root, { recursive: true });
const entries = [
  ['openai', 'openai', '4.0.0'], ['groq', 'groq-sdk', '0.3.0'],
  ['genai', '@google/genai', '1.33.0'], ['gemini', '@google/generative-ai', '0.1.0'],
  ['vertex', '@google-cloud/vertexai', '1.0.0'], ['bedrock', '@aws-sdk/client-bedrock-runtime', '3.693.0'],
  // Mistral typed image chunks start in the 1.x SDK; 0.x clients are text-only and not advertised.
  ['mistral', '@mistralai/mistralai', '1.0.0'], ['portkey', 'portkey-ai', '1.0.0'],
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
