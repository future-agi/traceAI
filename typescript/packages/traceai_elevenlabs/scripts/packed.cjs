// Stage 1 installs published dependencies; stage 2 runs with loopback-only networking.
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const [stage, directory, tarball] = process.argv.slice(2);
const root = path.resolve(__dirname, "..");
function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    cwd: directory,
    ...options,
  });
  if (result.error) throw result.error;
  if (result.status !== 0)
    throw new Error(`${command} exited ${result.status}`);
}
if (stage === "prepare") {
  const snippet = fs.readFileSync(path.join(root, "test/customer.ts"), "utf8").trim();
  if (!fs.readFileSync(path.join(root, "README.md"), "utf8").includes(snippet))
    throw new Error("README customer snippet must match the tested TypeScript source");
  fs.mkdirSync(directory, { recursive: true });
  if (fs.readdirSync(directory).length)
    throw new Error("Use a new empty consumer directory");
  const pkg = JSON.parse(
    fs.readFileSync(path.join(root, "package.json"), "utf8"),
  );
  const dependencies = {
    ...pkg.devDependencies,
    "@traceai/elevenlabs": path.resolve(tarball),
    "@traceai/fi-core": "^1.1.0",
    "@traceai/fi-semantic-conventions": "^1.0.0",
    "@opentelemetry/api": "^1.9.0",
    "@opentelemetry/core": "^2.0.1",
  };
  delete dependencies.eslint;
  delete dependencies["typescript-eslint"];
  fs.writeFileSync(
    path.join(directory, "package.json"),
    JSON.stringify(
      { name: "elevenlabs-packed-consumer", private: true, dependencies },
      null,
      2,
    ),
  );
  run("npm", ["install", "--ignore-scripts", "--no-audit", "--no-fund"]);
  fs.mkdirSync(path.join(directory, "test"));
  for (const name of [
    "byob.test.cjs",
    "contract.test.cjs",
    "edge.test.cjs",
    "fixture.cjs",
    "customer.ts",
  ])
    fs.copyFileSync(
      path.join(root, "test", name),
      path.join(directory, "test", name),
    );
  fs.copyFileSync(
    path.join(root, "scripts", "customer-check.cjs"),
    path.join(directory, "customer-check.cjs"),
  );
} else if (stage === "test") {
  const tsc = path.join(directory, "node_modules/typescript/bin/tsc");
  const common = [
    tsc,
    path.join(directory, "test/customer.ts"),
    "--target",
    "ES2022",
    "--lib",
    "ES2022,DOM,DOM.Iterable,DOM.AsyncIterable",
    "--moduleResolution",
    "node",
    "--esModuleInterop",
    "--skipLibCheck",
    "--strict",
  ];
  run(process.execPath, [
    ...common,
    "--module",
    "commonjs",
    "--outDir",
    path.join(directory, "cjs"),
  ]);
  run(process.execPath, [
    ...common,
    "--module",
    "es2022",
    "--outDir",
    path.join(directory, "esm"),
  ]);
  fs.writeFileSync(
    path.join(directory, "esm/package.json"),
    '{"type":"module"}',
  );
  run(
    process.execPath,
    [
      "--test",
      path.join(directory, "test/byob.test.cjs"),
      path.join(directory, "test/contract.test.cjs"),
      path.join(directory, "test/edge.test.cjs"),
    ],
    { env: { ...process.env, TRACEAI_PACKAGE: "@traceai/elevenlabs" } },
  );
  run(process.execPath, [path.join(directory, "customer-check.cjs")], {
    env: { ...process.env, TRACEAI_PACKAGE: "@traceai/elevenlabs" },
  });
} else
  throw new Error(
    "Use prepare <empty-directory> <tarball>, then test <directory>",
  );
