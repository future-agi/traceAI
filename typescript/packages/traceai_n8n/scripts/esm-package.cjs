const { writeFileSync } = require("node:fs");

writeFileSync("dist/esm/package.json", '{"type":"module"}\n');
