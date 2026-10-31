const { writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
writeFileSync(resolve(__dirname, '../dist/esm/package.json'), '{"type":"module"}\n');
