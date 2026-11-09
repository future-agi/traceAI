const fs = require('node:fs');
for (const dir of ['dist/esm', 'dist/esnext']) {
  fs.writeFileSync(`${dir}/package.json`, '{"type":"module"}\n');
  // A single CJS implementation keeps init idempotent across mixed ESM/CJS apps.
  fs.writeFileSync(`${dir}/index.js`, 'export { init, shutdown, forceFlush, report, registerSecret } from "../src/index.js";\n');
  fs.writeFileSync(`${dir}/register.js`, 'import "../src/register.js";\n');
  for (const name of ['esbuild', 'vite', 'rollup', 'webpack', 'next']) {
    const named = name === 'next' ? 'withTraceAI' : name === 'webpack' ? 'traceAI, TraceAIPlugin' : 'traceAI';
    const defaultName = name === 'next' ? 'withTraceAI' : name === 'webpack' ? 'TraceAIPlugin' : 'traceAI';
    fs.writeFileSync(`${dir}/bundlers/${name}.js`, `import plugin from "../../src/bundlers/${name}.js";\nexport const { ${named} } = plugin;\nexport default plugin.${defaultName};\n`);
  }
}
// tsc reserializes JSON; protocol catalogs must remain byte-identical to the spec.
for (const dir of ['dist/src', 'dist/esm', 'dist/esnext']) {
  for (const name of ['wire-protocols.json', 'privacy.json', 'privacy-fixtures.json']) {
    fs.copyFileSync(`src/wire/${name}`, `${dir}/wire/${name}`);
  }
}
fs.chmodSync('dist/src/cli.js', 0o755);
