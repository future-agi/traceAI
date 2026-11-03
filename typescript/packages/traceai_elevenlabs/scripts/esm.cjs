const fs = require("node:fs");
const path = require("node:path");
const directory = path.join(__dirname, "../dist/esm");
// A single implementation owns the weak client/module maps for both entrypoints.
// Clear older dual-build output so rebuilds cannot ship a second implementation.
fs.rmSync(directory, { recursive: true, force: true });
fs.mkdirSync(directory, { recursive: true });
fs.writeFileSync(path.join(directory, "package.json"), '{"type":"module"}');
fs.writeFileSync(
  path.join(directory, "index.js"),
  'import api from "../src/index.js";\n' +
    'export const { ElevenLabsInstrumentation, VERSION } = api;\n',
);
fs.writeFileSync(
  path.join(directory, "index.d.ts"),
  'export * from "../src/index.js";\n',
);
