// Run after the agent builds, in E2B only. No registry publication or real credentials.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");
const { promisify } = require("node:util");
const exec = promisify(require("node:child_process").execFile);
const { readReleaseGraph, releaseClosure } = require("./helpers/release-graph.cjs");

async function main() {
  const root = path.resolve(__dirname, "../../../..");
  const evidence = path.resolve(process.argv[2] ?? "/work/release-evidence");
  await fs.mkdir(evidence, { recursive: true });
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "fi-published-only-"));
  const tarballs = path.join(temp, "tarballs");
  const consumer = path.join(temp, "consumer");
  await fs.mkdir(tarballs);
  await fs.mkdir(consumer);
  const { packages } = readReleaseGraph(root);
  const closure = releaseClosure(packages, ["@traceai/agents"]);
  const receipt = { packages: [], setup: [], held: [], authRequests: 0 };
  let server;
  try {
    const dependencies = {};
    for (const [name, pkg] of closure) {
      assert.notEqual(pkg.private, true, name);
      const before = new Set(await fs.readdir(tarballs));
      await exec("pnpm", ["pack", "--pack-destination", tarballs], {
        cwd: pkg.directory, timeout: 180000, maxBuffer: 8 * 1024 * 1024,
        env: { ...process.env, npm_config_ignore_scripts: "true" },
      });
      const added = (await fs.readdir(tarballs)).filter(file => !before.has(file));
      assert.equal(added.length, 1, name);
      const archive = path.join(tarballs, added[0]);
      const { stdout } = await exec("tar", ["-xOf", archive, "package/package.json"]);
      const packed = JSON.parse(stdout);
      assert.equal(packed.name, name);
      assert.notEqual(packed.private, true);
      for (const field of ["dependencies", "optionalDependencies", "peerDependencies"]) {
        for (const version of Object.values(packed[field] ?? {})) {
          assert.ok(!version.startsWith("workspace:"), `${name}: unresolved workspace version`);
        }
      }
      dependencies[name] = `file:${archive}`;
      receipt.packages.push({ name, version: packed.version,
        sha256: crypto.createHash("sha256").update(await fs.readFile(archive)).digest("hex") });
    }
    // File specs are only in the disposable consumer. Published manifests remain
    // untouched, including their normal semver dependency edges.
    await fs.writeFile(path.join(consumer, "package.json"), JSON.stringify({ private: true, dependencies }));
    const install = await exec("npm", ["install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], {
      cwd: consumer, timeout: 300000, maxBuffer: 8 * 1024 * 1024,
      env: { ...process.env, npm_config_cache: path.join(temp, "empty-cache") },
    });
    await fs.writeFile(path.join(evidence, "install.log"), install.stdout + install.stderr);
    const installed = createRequire(path.join(consumer, "package.json"));
    const { adapterRegistry } = installed("@traceai/agents");
    assert.ok((await fs.realpath(installed.resolve("@traceai/agents"))).startsWith(consumer + path.sep));
    const held = adapterRegistry.filter(entry => packages.get(entry.packageName)?.private);
    for (const entry of held) {
      assert.throws(() => installed.resolve(entry.packageName), { code: "MODULE_NOT_FOUND" });
    }
    const cli = path.join(consumer, "node_modules/.bin/fi-agent");
    const baseEnv = { PATH: path.dirname(process.execPath), HOME: path.join(temp, "home") };
    await fs.mkdir(baseEnv.HOME);
    const run = async (args, env = baseEnv) => {
      try { return { code: 0, ...await exec(cli, args, { cwd: consumer, env, timeout: 30000 }) }; }
      catch (error) {
        if (typeof error.code !== "number") throw error;
        return { code: error.code, stdout: error.stdout, stderr: error.stderr };
      }
    };
    const status = await run(["status", "--json"]);
    assert.equal(status.code, 2); // Listing is informational, not a connected-health assertion.
    assert.equal(status.stderr, "");
    receipt.status = JSON.parse(status.stdout);
    assert.equal(receipt.status.agents.length, adapterRegistry.length);
    const invalidRequests = [];
    server = http.createServer((req, res) => {
      let body = "";
      req.on("data", chunk => { body += chunk; });
      req.on("end", () => {
        receipt.authRequests++;
        const valid = req.method === "POST" && req.url === "/tracer/v1/traces" &&
          req.headers["x-api-key"] === "synthetic-release-api" &&
          req.headers["x-secret-key"] === "synthetic-release-secret" &&
          req.headers["x-project-name"] === "release-smoke" &&
          body === JSON.stringify({ resourceSpans: [] });
        if (!valid) invalidRequests.push("unexpected authentication request");
        res.writeHead(valid ? 200 : 401, { "Content-Type": "application/json" });
        res.end("{}");
      });
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const authEnv = {
      ...baseEnv, FI_API_KEY: "synthetic-release-api", FI_SECRET_KEY: "synthetic-release-secret",
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    };
    for (const entry of adapterRegistry.filter(entry => entry.qualified)) {
      assert.ok(closure.has(entry.packageName), entry.id);
      const home = path.join(temp, `home-${entry.id}`);
      await fs.mkdir(home);
      // Hermes normally discovers its selected host profile via `hermes`. An
      // explicit profile path keeps this configure-only package smoke host-free.
      const profile = entry.id === "hermes" ? ["--agent-dir", path.join(home, "hermes-profile")] : [];
      const result = await run(["setup", entry.id, "--configure-only", "--user-only", "--project", "release-smoke", "--json", ...profile], { ...authEnv, HOME: home });
      assert.equal(result.code, 0, `${entry.id}: ${result.stdout} ${result.stderr}`);
      const output = JSON.parse(result.stdout);
      assert.equal(output.configured, true, entry.id);
      assert.equal(output.registered, false, entry.id);
      assert.doesNotMatch(result.stdout + result.stderr, /synthetic-release-(api|secret)/);
      receipt.setup.push({ agent: entry.id, exitCode: result.code, code: output.code });
    }
    assert.equal(receipt.authRequests, receipt.setup.length);
    assert.deepEqual(invalidRequests, []);
    for (const entry of held) {
      for (const command of ["status", "setup"]) {
        const result = await run([command, entry.id, "--configure-only", "--json"], authEnv);
        assert.equal(result.code, 2);
        assert.equal(result.stderr, "");
        assert.equal(JSON.parse(result.stdout).code, entry.reason);
        receipt.held.push({ agent: entry.id, command, code: entry.reason });
      }
    }
    assert.equal(receipt.authRequests, receipt.setup.length, "held setup attempted auth");
    assert.deepEqual(await fs.readdir(baseEnv.HOME), [], "held setup changed home");
    receipt.result = "PASS";
    await fs.writeFile(path.join(evidence, "tarball-receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    console.log(JSON.stringify({ result: receipt.result, publicPackages: receipt.packages.length,
      qualifiedSetups: receipt.setup.length, heldChecks: receipt.held.length, authRequests: receipt.authRequests }));
  } finally {
    if (server) await new Promise(resolve => server.close(resolve));
    await fs.rm(temp, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
