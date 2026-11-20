const fs = require('node:fs');
// tsc does not emit privacy.json (it is not in any tsconfig include), so these byte-exact copies are
// the only ones: a consumer's `tsc --build` reference to fi-core cannot reserialize them.
for (const dir of ['dist/src', 'dist/esm', 'dist/esnext']) {
  fs.copyFileSync('src/privacy.json', `${dir}/privacy.json`);
}
// The ESM shims re-export exactly the compiled CommonJS module's export names, so a new runtime export
// cannot go missing from the ESM entry points.
const runtimeExports = Object.keys(require('../dist/src/privacy-runtime.js'));
if (!runtimeExports.length || !runtimeExports.every(name => /^[A-Za-z_$][\w$]*$/.test(name))) throw new Error('postbuild: unexpected privacy-runtime exports');
for (const dir of ['dist/esm', 'dist/esnext']) {
  fs.writeFileSync(`${dir}/package.json`, '{"type":"module"}\n');
  // Keep a single implementation across module formats, and avoid Node's JSON
  // import-attribute requirement. Browser bundlers can still inline this leaf.
  for (const name of ['privacy', 'privacy-runtime']) {
    fs.writeFileSync(`${dir}/${name}.js`, `export { ${runtimeExports.join(', ')} } from "../src/privacy-runtime.js";\n`);
  }
}

// A provider and an adapter may import different module formats. Keep correlation
// class identity (and its private span references) identical across those imports.
for (const name of ['index', 'manager', 'correlation', 'policy', 'transport-v1', 'node', 'disabled', 'admission', 'stream', 'recording', 'imports', 'owned', 'node-sources', 'wire']) {
  const exports = Object.keys(require(`../dist/src/attachments/${name}.js`));
  if (!exports.every(key => /^[A-Za-z_$][\w$]*$/.test(key))) throw new Error('postbuild: unexpected attachment export');
  for (const dir of ['dist/esm', 'dist/esnext']) {
    fs.writeFileSync(`${dir}/attachments/${name}.js`, `export { ${exports.join(', ')} } from "../../src/attachments/${name}.js";\n`);
  }
}
