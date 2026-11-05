const path = require("node:path");
const { createRequire } = require("node:module");
const assert = require("node:assert/strict");
const base = require("../jest.config.js");

assert.ok(process.env.LANGCHAIN_CONSUMER, "LANGCHAIN_CONSUMER must select an isolated consumer");
assert.ok(process.env.CORE_VERSION, "CORE_VERSION must select the matrix version");
const consumerRequire = createRequire(path.join(process.env.LANGCHAIN_CONSUMER, "package.json"));
assert.equal(consumerRequire("@langchain/core/package.json").version, process.env.CORE_VERSION);
assert.equal(consumerRequire("@langchain/openai/package.json").version, process.env.OPENAI_VERSION);

module.exports = {
  ...base,
  rootDir: path.resolve(__dirname, ".."),
  resolver: path.join(__dirname, "runtime-resolver.cjs"),
  // This is a runtime matrix, not an internal-source type compatibility promise.
  // Build with locked declarations; separately type-check the packed public API
  // against each consumer's declarations in types.mts/types.cts.
  transform: {
    "^.+\\.ts$": ["ts-jest", {
      diagnostics: false,
      tsconfig: { module: "commonjs", isolatedModules: true },
    }],
  },
};
