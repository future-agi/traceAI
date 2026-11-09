// Run only inside the isolated E2B fresh install. Not a real Cursor IDE qualification.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { spawn, spawnSync } = require("node:child_process");
function runHook(command, payload, env) {
  return new Promise((resolve, reject) => {
    const process = spawn("/bin/sh", ["-c", command], { env });
    let stdout = "",
      stderr = "";
    process.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    process.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    process.on("error", reject);
    process.on("exit", (code) => {
      try {
        assert.equal(code, 0);
        assert.equal(stdout, "");
        assert.equal(stderr, "");
        resolve();
      } catch (error) {
        reject(error);
      }
    });
    process.stdin.end(JSON.stringify(payload));
  });
}
async function main() {
  const root = "/work/clean-install";
  const core = require(path.join(root, "node_modules/@traceai/agent-core"));
  const cursor = require(
    path.join(root, "node_modules/@traceai/cursor-plugin"),
  );
  const registration = require(
    path.join(root, "node_modules/@traceai/cursor-plugin/dist/registration.js"),
  );
  const cli = path.join(root, "node_modules/@traceai/agents/dist/cli.js");
  const home = "/work/packed-home";
  await fs.mkdir(home, { recursive: true, mode: 0o700 });
  const env = {
    HOME: home,
    PATH: "/missing",
    FI_AGENT_CONFIG_DIR: path.join(home, ".config/futureagi/agents"),
  };
  const help = spawnSync(process.execPath, [cli, "--help"], {
    env,
    encoding: "utf8",
  });
  assert.equal(help.status, 0);
  assert.match(help.stdout, /setup/);
  const status = spawnSync(
    process.execPath,
    [cli, "status", "cursor", "--json"],
    { env, encoding: "utf8" },
  );
  assert.equal(status.status, 2);
  assert.equal(JSON.parse(status.stdout).qualification, "QUALIFIED");
  assert.equal(cursor.qualification.live, true);
  const pkg = JSON.parse(
    await fs.readFile(
      path.join(root, "node_modules/@traceai/cursor-plugin/package.json"),
    ),
  );
  assert.notEqual(pkg.private, true);
  assert.equal(pkg.publishConfig.access, "public");
  const hooks = JSON.parse(
    await fs.readFile(
      path.join(root, "node_modules/@traceai/cursor-plugin/hooks/hooks.json"),
    ),
  );
  assert.deepEqual(hooks.hooks, {});
  const requests = [];
  const server = http.createServer((q, r) => {
    let body = "";
    q.on("data", (chunk) => {
      body += chunk;
    });
    q.on("end", () => {
      requests.push(JSON.parse(body));
      r.setHeader("Content-Type", "application/json");
      r.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    await core.setup({
      agent: "cursor",
      home,
      env,
      credentials: { apiKey: "dummy-pack-api", secretKey: "dummy-pack-secret" },
      projectName: "pack-test",
      configureOnly: true,
      otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    });
    const config = await core.resolveConfig({ agent: "cursor", home, env });
    const runtime = await registration.installRuntime({
      config,
      scope: "user",
    });
    const result = await registration.register(
      { config, scope: "user" },
      runtime,
    );
    assert.equal(result.registered, true);
    const hook = registration.commands(runtime, config.paths.configDir).stop
      .command;
    const resultHook = spawnSync("/bin/sh", ["-c", hook], {
      env: { ...env, FI_AGENT_ENABLED: "false" },
      input: "{}",
      encoding: "utf8",
    });
    assert.equal(resultHook.status, 0);
    assert.equal(resultHook.stdout, "");
    assert.equal(resultHook.stderr, "");
    // Exercise only the newly installed runtime bundle, with real run4 and cli2 hooks.
    const {
      load,
      spans,
      prepare,
      checkGolden,
    } = require("./round2-golden.cjs");
    const installedHooks = registration.commands(
      runtime,
      config.paths.configDir,
    );
    const golden = [];
    for (const label of ["run4", "cli", "cli2", "cli-print"]) {
      const transcript = await prepare(home, label, label === "cli2");
      const before = requests.length;
      let writer;
      try {
        for (const row of load(label)) {
          if (label === "cli2" && row.hook_event_name === "stop")
            writer = new Promise((r) => setTimeout(r, 6000)).then(() =>
              fs.appendFile(transcript.file, transcript.tail),
            );
          await runHook(
            installedHooks[row.hook_event_name].command,
            {
              ...row,
              workspace_roots: [home],
              transcript_path: row.transcript_path ? transcript.file : null,
            },
            env,
          );
        }
      } finally {
        if (writer) await writer;
      }
      if (writer) {
        const deadline = Date.now() + 5000;
        while (
          spans(requests.slice(before)).length < 11 &&
          Date.now() < deadline
        )
          await new Promise((r) => setTimeout(r, 50));
      }
      golden.push(checkGolden(spans(requests.slice(before)), label));
    }
    const {
      terminalCases,
      terminalRegression,
      structuredRedactionRegression,
    } = require("./review-regressions.cjs");
    const installed = {
      home,
      readSpans: () => spans(requests),
      readDiagnostics: async () =>
        JSON.parse(
          await fs.readFile(
            path.join(config.paths.stateDir, "cursor-diagnostics.json"),
            "utf8",
          ),
        ),
      send: (row, extraEnv = {}) =>
        runHook(
          installedHooks[row.hook_event_name].command,
          { ...row, workspace_roots: [home] },
          { ...env, ...extraEnv },
        ),
    };
    const regressions = [];
    for (const name of terminalCases)
      regressions.push(await terminalRegression(installed, name));
    regressions.push(await structuredRedactionRegression(installed));
    const {
      fragmentRedactionCases,
      fragmentRedactionRegression,
      errorContextRegression,
      boundaryCases,
      boundaryRegression,
    } = require("./round3-regressions.cjs");
    for (const variant of fragmentRedactionCases)
      regressions.push(await fragmentRedactionRegression(installed, variant));
    for (const hide of [true, false])
      regressions.push(await errorContextRegression(installed, hide));
    for (const scenario of boundaryCases)
      regressions.push(await boundaryRegression(installed, scenario));
    const receipt = {
      test: "fresh-cache-local-tarball-install",
      passed: true,
      publicRegistryInstallation: false,
      realCursorIDE: false,
      lateTranscriptWriteMs: 6000,
      golden,
      regressions,
    };
    await fs.writeFile(
      "/work/packed-golden.json",
      JSON.stringify(receipt, null, 2) + "\n",
    );
    console.log(JSON.stringify(receipt));
  } finally {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
