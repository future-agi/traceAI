#!/usr/bin/env node
'use strict';
const { main } = require('../dist/src/cli/main.js');
void main(process.argv.slice(2), {
  version: require('../package.json').version,
  entry: __filename,
  storageEntry: require.resolve('../dist/src/runtime/storage-child.js'),
}).then(code => { process.exitCode = code; }, () => {
  process.stderr.write('LOCAL_UNAVAILABLE\n'); process.exitCode = 7;
});
