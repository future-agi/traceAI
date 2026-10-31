// Synthetic CLI contracts run on CI hosts too; this does NOT expand production support.
const path = require("node:path");
const { main } = require("../../dist/src/cli/main.js");
const { AdapterRegistry } = require("../../dist/src/adapters.js");
const {
  claudeCodeAdapter,
} = require("../../dist/src/adapters/claude-code/index.js");
const adapter = {
  ...claudeCodeAdapter,
  capability: {
    ...claudeCodeAdapter.capability,
    platforms: [process.platform],
  },
};
const settings = {
  version: "0.1.0",
  entry: __filename,
  storageEntry: path.resolve(
    __dirname,
    "../../dist/src/runtime/storage-child.js",
  ),
  registry: new AdapterRegistry([adapter]),
};
module.exports = { adapter, settings };
if (require.main === module)
  main(process.argv.slice(2), settings).then((code) => {
    process.exitCode = code;
  });
