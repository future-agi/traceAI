const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { releaseClosure, readReleaseGraph } = require("./helpers/release-graph.cjs");

const root = path.resolve(__dirname, "../../../..");

test("every npm release root has a public runtime dependency closure", () => {
  const { closure, roots } = readReleaseGraph(root);
  assert.ok(roots.includes("@traceai/agents"));
  assert.ok(closure.size >= roots.length);
  for (const pkg of closure.values()) assert.notEqual(pkg.private, true, pkg.name);
});

test("CLI installs each publishable integration as a regular dependency", () => {
  const { packages } = readReleaseGraph(root);
  const cli = packages.get("@traceai/agents");
  const { adapterRegistry } = require("../dist/registry.js");
  for (const entry of adapterRegistry) {
    const pkg = packages.get(entry.packageName);
    if (!pkg) continue; // Registry placeholders do not claim an implementation.
    if (pkg.private) {
      for (const field of ["dependencies", "optionalDependencies", "peerDependencies", "devDependencies"]) {
        assert.equal(cli[field]?.[pkg.name], undefined, `${field}: ${entry.id}`);
      }
    } else if (entry.qualified) {
      assert.equal(cli.dependencies[pkg.name], "workspace:^", entry.id);
    }
  }
});

for (const field of ["dependencies", "optionalDependencies", "peerDependencies"]) {
  test(`closure rejects transitive private ${field}, including npm aliases`, () => {
    for (const alias of [false, true]) {
      const packages = new Map([
        ["cli", { dependencies: { adapter: "workspace:^" } }],
        ["adapter", { [field]: alias ? { publicAlias: "npm:@traceai/held@0.1.0" } : { "@traceai/held": "workspace:^" } }],
        ["@traceai/held", { private: true }],
      ]);
      assert.throws(() => releaseClosure(packages, ["cli"]), /Private release dependency: cli -> adapter -> @traceai\/held/);
    }
  });
}

test("closure rejects private bundled dependencies and private roots", () => {
  const packages = new Map([
    ["cli", { bundledDependencies: ["held"] }], ["held", { private: true }],
  ]);
  assert.throws(() => releaseClosure(packages, ["cli"]), /Private release dependency: cli -> held/);
  assert.throws(() => releaseClosure(packages, ["held"]), /Private release dependency: held/);
});

test("closure handles cycles, excludes build-only dependencies and rejects unresolved workspace edges", () => {
  const packages = new Map([
    ["a", { dependencies: { b: "workspace:^" }, devDependencies: { held: "workspace:^" } }],
    ["b", { dependencies: { a: "workspace:^", external: "1.2.3" } }],
    ["held", { private: true }],
  ]);
  assert.deepEqual([...releaseClosure(packages, ["a"]).keys()], ["a", "b"]);
  packages.get("b").dependencies.missing = "workspace:^";
  assert.throws(() => releaseClosure(packages, ["a"]), /Missing workspace dependency: a -> b -> missing/);
});
