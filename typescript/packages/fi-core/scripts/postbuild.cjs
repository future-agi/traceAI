const fs = require('node:fs');
for (const dir of ['dist/src', 'dist/esm', 'dist/esnext']) {
  fs.copyFileSync('src/privacy.json', `${dir}/privacy.json`);
}
for (const dir of ['dist/esm', 'dist/esnext']) {
  fs.writeFileSync(`${dir}/package.json`, '{"type":"module"}\n');
  // Keep a single implementation across module formats, and avoid Node's JSON
  // import-attribute requirement. Browser bundlers can still inline this leaf.
  for (const name of ['privacy', 'privacy-runtime']) {
    fs.writeFileSync(`${dir}/${name}.js`, 'export { MAX_DISCOVERY_BODY_BYTES, CREDENTIAL_HEADERS, normalizePrivacyName, isCredentialName, mediaReplacement, indexPrivacyHints, scrubText, scrubValue, scrubAttribute, discoverSecrets, credentialSecrets } from "../src/privacy-runtime.js";\n');
  }
}
