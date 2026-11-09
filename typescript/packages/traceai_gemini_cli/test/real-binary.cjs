// Run only in E2B, after packing/installing the exact artifacts. Lifecycle qualification, not Gemini model transport.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
async function run(command, args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      ...options,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (b) => (stdout += b));
    child.stderr.on("data", (b) => (stderr += b));
    child.on("error", reject);
    const timer = setTimeout(() => child.kill("SIGKILL"), 90000);
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}
(async () => {
  const install = process.argv[2],
    binary = process.argv[3],
    out = process.argv[4];
  await fs.mkdir(out, { recursive: true });
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-gemini-real-")),
    project = path.join(home, "project");
  await fs.mkdir(project, { mode: 0o700 });
  await fs.mkdir(path.join(home, ".gemini"), { mode: 0o700 });
  const captures = [],
    modelRequests = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (b) => (raw += b));
    req.on("end", () => {
      if (req.url === "/tracer/v1/traces") {
        captures.push({ path: req.url, body: JSON.parse(raw) });
        res.setHeader("content-type", "application/json");
        res.end("{}");
      } else {
        modelRequests.push(req.url);
        res.statusCode = 503;
        res.end("Test rejects every model transport request");
      }
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const env = {
    PATH: path.dirname(binary) + ":" + process.env.PATH,
    HOME: home,
    TERM: "dumb",
    NO_COLOR: "1",
    CI: "true",
    GEMINI_API_KEY: "synthetic-not-a-google-key",
    GOOGLE_GEMINI_BASE_URL: base,
    GEMINI_TELEMETRY_ENABLED: "false",
    GEMINI_CLI_NO_RELAUNCH: "true",
    FI_API_KEY: "synthetic-fi-api",
    FI_SECRET_KEY: "synthetic-fi-secret",
    FI_OTLP_ENDPOINT: base + "/tracer/v1/traces",
    NODE_OPTIONS: `--require=${path.join(__dirname, "loopback-only.cjs")}`,
    EGRESS_EVIDENCE: path.join(out, "egress.txt"),
  };
  const cli = path.join(
    install,
    "node_modules",
    "@traceai",
    "agents",
    "dist",
    "cli.js",
  );
  const settings = {
    security: { auth: { selectedType: "gemini-api-key" } },
    telemetry: { enabled: false },
    hooks: {
      BeforeModel: [
        {
          matcher: "*",
          hooks: [
            {
              name: "synthetic-test-only",
              type: "command",
              command: `'${process.execPath}' '${path.join(__dirname, "synthetic-model.cjs")}'`,
              timeout: 3000,
            },
          ],
        },
      ],
    },
  };
  await fs.writeFile(
    path.join(home, ".gemini", "settings.json"),
    JSON.stringify(settings),
    { mode: 0o600 },
  );
  const evidence = {
    hostVersion: "0.67.0",
    qualification:
      "real-binary synthetic BeforeModel lifecycle; NOT model transport",
    setup: [],
    runs: [],
    modelRequests,
    spans: [],
  };
  try {
    for (let i = 0; i < 2; i++) {
      const result = await run(
        process.execPath,
        [
          cli,
          "setup",
          "gemini",
          "--project",
          "gemini-e2b",
          "--project-dir",
          project,
          "--json",
        ],
        { env, cwd: project },
      );
      evidence.setup.push(result);
      assert.equal(result.code, 2, result.stdout + " " + result.stderr);
      assert.equal(JSON.parse(result.stdout).registered, true);
      assert.equal(JSON.parse(result.stdout).code, "TRUST_REQUIRED");
    }
    // The installer must not grant workspace trust. Qualify the real refusal first, then supply an explicit test-only exact-directory trust fixture.
    const untrusted = await run(
      binary,
      ["-p", "Untrusted lifecycle probe", "--model", "gemini-2.5-flash"],
      { env, cwd: project },
    );
    evidence.untrustedRun = untrusted;
    assert.equal(untrusted.code, 55);
    await assert.rejects(
      fs.access(path.join(home, ".gemini", "trustedFolders.json")),
    );
    await fs.writeFile(
      path.join(home, ".gemini", "trustedFolders.json"),
      JSON.stringify({ [project]: "TRUST_FOLDER" }),
      { mode: 0o600 },
    );
    for (let i = 0; i < 2; i++) {
      const result = await run(
        binary,
        [
          "-p",
          `Reply with a public lifecycle greeting ${i}`,
          "--model",
          "gemini-2.5-flash",
          "--output-format",
          "json",
        ],
        { env, cwd: project },
      );
      evidence.runs.push(result);
      assert.equal(result.code, 0, result.stderr);
      assert.match(result.stdout, /SYNTHETIC_LIFECYCLE_OK/);
    }
    evidence.spans = captures.flatMap((r) =>
      r.body.resourceSpans.flatMap((rs) =>
        rs.scopeSpans.flatMap((ss) => ss.spans),
      ),
    );
    const attrs = (s) =>
      Object.fromEntries(
        (s.attributes || []).map((a) => [a.key, Object.values(a.value)[0]]),
      );
    const roots = evidence.spans.filter((s) => !s.parentSpanId);
    assert.equal(roots.length, 2, "Each real prompt must export one root");
    assert.equal(
      evidence.spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      0,
      "Synthetic override must not invent model calls",
    );
    assert.equal(
      modelRequests.length,
      0,
      "No model transport calls are allowed",
    );
    evidence.status = await run(
      process.execPath,
      [cli, "status", "gemini", "--json"],
      { env, cwd: project },
    );
    assert.equal(JSON.parse(evidence.status.stdout).activation, "OBSERVED");
    const disabled = await run(
      process.execPath,
      [cli, "disable", "gemini", "--json"],
      { env, cwd: project },
    );
    assert.equal(disabled.code, 0);
    const prior = captures.length;
    const result = await run(
      binary,
      [
        "-p",
        "Reply with a public disabled greeting",
        "--model",
        "gemini-2.5-flash",
        "--output-format",
        "json",
      ],
      { env, cwd: project },
    );
    assert.equal(result.code, 0, result.stderr);
    assert.equal(captures.length, prior, "disabled route must not export");
    evidence.disabledRun = result;
    evidence.result = "PASS_LIFECYCLE_ONLY";
  } catch (e) {
    evidence.result = "FAIL";
    evidence.error = e.message;
    process.exitCode = 1;
  } finally {
    await fs.writeFile(
      path.join(out, "evidence.json"),
      JSON.stringify(evidence, null, 2),
    );
    await fs.writeFile(
      path.join(out, "capture.json"),
      JSON.stringify(captures, null, 2),
    );
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  }
  console.log(
    JSON.stringify({
      result: evidence.result,
      roots: evidence.spans.filter((s) => !s.parentSpanId).length,
      modelRequests: modelRequests.length,
      error: evidence.error,
    }),
  );
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
