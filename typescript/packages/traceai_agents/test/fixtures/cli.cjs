// Synthetic fixture executable only; excluded from npm artifacts.
const path = require("node:path");
const { main } = require("../../dist/src/cli/main.js");
const { AdapterRegistry } = require("../../dist/src/adapters.js");
const { AgentError } = require("../../dist/src/security/files.js");
const reference = require("../../contract/reference.cjs");
const adapter = {
  ...reference,
  launch: async (_context, { tracing }) => {
    if (process.env.FIXTURE_CONFLICT)
      throw new AgentError("NATIVE_DESTINATION_CONFLICT", 6);
    return {
      executable: process.execPath,
      args: [],
      env: process.env.FIXTURE_UNSAFE_OVERLAY
        ? { OPENAI_BASE_URL: "http://127.0.0.1:1" }
        : { OTEL_TRACES_EXPORTER: "otlp" },
    };
  },
};
const other = {
  ...reference,
  capability: { ...reference.capability, surface: "other" },
};
const settings = {
  version: "0.1.0",
  entry: process.env.FIXTURE_UNAVAILABLE
    ? path.join(process.env.HOME, "missing-runtime.cjs")
    : __filename,
  storageEntry: path.resolve(
    __dirname,
    "../../dist/src/runtime/storage-child.js",
  ),
  registry: new AdapterRegistry([adapter, other]),
};
module.exports = { settings, adapter, other };
if (require.main === module) {
  main(process.argv.slice(2), settings).then((code) => {
    process.exitCode = code;
    if (process.argv[2] === "__daemon") process.send?.({ ready: code === 0 });
  });
}
