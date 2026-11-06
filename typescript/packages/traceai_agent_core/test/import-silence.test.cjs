const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

test("loading protected locks remains silent in CJS and ESM on supported Node versions", () => {
  for (const [entry, load] of [
    ["../dist/file-lock.js", (file) => `require(${JSON.stringify(file)})`],
    ["../dist/esm/file-lock.js", (file) => `import(${JSON.stringify(pathToFileURL(file).href)})`],
  ]) {
    const result = spawnSync(process.execPath, ["--trace-deprecation", "-e", load(path.resolve(__dirname, entry))], {
      encoding: "utf8",
      env: { PATH: process.env.PATH },
      timeout: 10000,
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "", entry);
    assert.equal(result.stderr, "", entry);
  }
});
