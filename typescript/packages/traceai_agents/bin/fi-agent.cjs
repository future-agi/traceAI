#!/usr/bin/env node
"use strict";
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { codexAdapter } = require("../dist/src/adapters/codex/index.js");
const { main } = require("../dist/src/cli/main.js");
void main(process.argv.slice(2), {
  version: require("../package.json").version,
  entry: __filename,
  registry: new AdapterRegistry([
    codexAdapter(
      require.resolve("./fi-agent-codex-hook.cjs"),
      require("../package.json").version,
    ),
  ]),
  storageEntry: require.resolve("../dist/src/runtime/storage-child.js"),
}).then(
  (code) => {
    process.exitCode = code;
  },
  () => {
    process.stderr.write("LOCAL_UNAVAILABLE\n");
    process.exitCode = 7;
  },
);
