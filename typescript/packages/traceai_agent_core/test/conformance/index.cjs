// Test-only contract. Never imported by the runtime entrypoint.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
const core = require("../../dist/src/index.js");
const MARKERS = Object.freeze({
  input: "CONF_INPUT_19275",
  output: "CONF_OUTPUT_19275",
  tool: "CONF_TOOL_OUTPUT_19275",
  error: "CONF_ERROR_19275",
  secret: "sk-abcdefghijklmnopqrstuvwxyz1234567890",
});
const FLAGS = [
  "FI_HIDE_INPUTS",
  "FI_HIDE_INPUT_TEXT",
  "FI_HIDE_INPUT_MESSAGES",
  "FI_HIDE_INPUT_IMAGES",
  "FI_HIDE_OUTPUTS",
  "FI_HIDE_OUTPUT_TEXT",
  "FI_HIDE_OUTPUT_MESSAGES",
  "FI_HIDE_ERROR_DETAILS",
];
const packages = path.resolve(__dirname, "../../..");
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const attributes = (span) =>
  Object.fromEntries(
    (span.attributes || []).map((a) => [
      a.key,
      a.value.stringValue ??
        a.value.intValue ??
        a.value.doubleValue ??
        a.value.boolValue,
    ]),
  );
const spans = (batches) =>
  batches.flatMap((b) =>
    (b.resourceSpans || []).flatMap((r) =>
      (r.scopeSpans || []).flatMap((s) => s.spans || []),
    ),
  );
function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const p = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "",
      stderr = "",
      timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      p.kill("SIGKILL");
    }, options.timeout || 45000);
    p.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    p.stdout.on("data", (b) => (stdout += b));
    p.stderr.on("data", (b) => (stderr += b));
    p.stdin.on("error", () => {});
    p.stdin.end(options.input);
    p.on("close", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr, timedOut });
    });
  });
}
async function fixture(agent, overrides = {}, decodeRequest) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "traceai-conformance-"));
  const project = path.join(home, "project");
  await fs.mkdir(project, { mode: 0o700 });
  const batches = [],
    requests = [];
  let rejectAuth = false;
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const b of req) chunks.push(b);
      const bytes = Buffer.concat(chunks);
      const body =
        req.headers["content-type"]?.includes("protobuf") && decodeRequest
          ? await decodeRequest(bytes)
          : JSON.parse(bytes.toString() || "{}");
      requests.push({
        url: req.url,
        body,
        authPresent: !!(
          req.headers["x-api-key"] && req.headers["x-secret-key"]
        ),
        authMatches:
          req.headers["x-api-key"] === "synthetic-api" &&
          req.headers["x-secret-key"] === "synthetic-secret",
      }); // Never retain credential headers.
      if (!rejectAuth) batches.push(body);
      const protobuf = req.headers["content-type"]?.includes("protobuf");
      res
        .writeHead(rejectAuth ? 401 : 200, {
          "content-type": protobuf
            ? "application/x-protobuf"
            : "application/json",
        })
        .end(protobuf ? Buffer.alloc(0) : "{}");
    } catch {
      res.writeHead(400).end("{}");
    }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const old = { ...process.env };
  // Whitelist host-independent environment. No developer credentials or real host directories.
  const env = {
    PATH: old.PATH,
    HOME: home,
    TMPDIR: home,
    NO_COLOR: "1",
    XDG_CONFIG_HOME: path.join(home, ".config"),
    XDG_STATE_HOME: path.join(home, ".local/state"),
    XDG_DATA_HOME: path.join(home, ".local/share"),
    FI_AGENT_CONFIG_DIR: path.join(home, ".config/futureagi/agents"),
    CLAUDE_CONFIG_DIR: path.join(home, ".claude"),
    CODEX_HOME: path.join(home, ".codex"),
    HERMES_HOME: path.join(home, ".hermes"),
    GOOSE_PATH_ROOT: path.join(home, "goose"),
    OPENCLAW_STATE_DIR: path.join(home, ".openclaw"),
    ...overrides,
  };
  for (const k of Object.keys(process.env)) delete process.env[k];
  Object.assign(process.env, env);
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const options = { agent, home, env, projectDir: project };
  await core.setup({
    ...options,
    configureOnly: true,
    credentials: { apiKey: "synthetic-api", secretKey: "synthetic-secret" },
    otlpEndpoint: endpoint,
    projectName: "conformance",
  });
  const config = await core.resolveConfig(options);
  const cleanup = [];
  return {
    agent,
    home,
    project,
    env,
    config,
    options,
    endpoint,
    batches,
    requests,
    cleanup,
    core,
    setAuthFailure(v) {
      rejectAuth = v;
    },
    cli(args, extra = {}) {
      return run(
        process.execPath,
        [path.join(packages, "traceai_agents/dist/cli.js"), ...args],
        { cwd: project, env: { ...env, ...extra } },
      );
    },
    async close() {
      try {
        for (const fn of cleanup.reverse()) await fn();
      } finally {
        server.closeAllConnections();
        await new Promise((r) => server.close(r));
        for (const k of Object.keys(process.env)) delete process.env[k];
        Object.assign(process.env, old);
        await fs.rm(home, {
          recursive: true,
          force: true,
          maxRetries: 20,
          retryDelay: 100,
        });
      }
    },
  };
}
function checkShape(batches, expected) {
  const all = spans(batches),
    roots = all.filter((s) => !s.parentSpanId),
    llms = all.filter(
      (s) =>
        (attributes(s)["fi.span.kind"] ?? attributes(s)["gen_ai.span.kind"]) ===
        "LLM",
    ),
    tools = all.filter(
      (s) =>
        (attributes(s)["fi.span.kind"] ?? attributes(s)["gen_ai.span.kind"]) ===
        "TOOL",
    );
  assert.equal(roots.length, expected.turns || 1, `roots=${roots.length}`);
  assert.equal(
    new Set(all.map((s) => `${s.traceId}:${s.spanId}`)).size,
    all.length,
    "duplicate exported span IDs",
  );
  for (const s of all)
    if (s.parentSpanId)
      assert(
        all.some((p) => p.spanId === s.parentSpanId && p.traceId === s.traceId),
        `orphan ${s.name}`,
      );
  assert.equal(
    llms.length,
    expected.calls,
    `generations=${llms.length}, expected=${expected.calls}`,
  );
  assert.equal(
    tools.length,
    expected.tools,
    `tools=${tools.length}, expected=${expected.tools}`,
  );
  const childAgents = all.filter(s => s.parentSpanId &&
    (attributes(s)["fi.span.kind"] ?? attributes(s)["gen_ai.span.kind"]) === "AGENT");
  assert.equal(childAgents.length, expected.childAgents ?? 0, "non-root agent spans");
  assert.equal(all.length, roots.length + llms.length + tools.length + childAgents.length, "unexpected span shape");
  for (const s of llms) {
    const model =
      attributes(s)["gen_ai.request.model"] ||
      attributes(s)["gen_ai.response.model"];
    assert(model, "generation missing model");
    if (expected.model && (!expected.requestUsage || expected.requestIdentity === "order")) assert.equal(model, expected.model, "generation model");
  }
  for (const s of tools) {
    assert(attributes(s)["tool.name"], "tool missing tool.name");
    if (expected.toolOutcome) {
      if (attributes(s)["fi.agent.outcome"] !== undefined)
        assert.equal(attributes(s)["fi.agent.outcome"], expected.toolOutcome, "tool outcome");
      assert.equal(({STATUS_CODE_OK:1, STATUS_CODE_ERROR:2})[s.status?.code] ?? s.status?.code, expected.toolOutcome === "error" ? 2 : 1, "tool status code");
    }
  }
  for (const r of roots) {
    assert(attributes(r)["session.id"], "root missing session.id");
    assert.equal(
      attributes(r)["fi.agent.outcome"],
      expected.outcome || "success",
      "root outcome",
    );
  }
  const sum = (key) =>
    all.reduce((n, s) => n + Number(attributes(s)[key] || 0), 0);
  assert.equal(
    sum("gen_ai.usage.input_tokens"),
    expected.inputTokens ?? expected.calls * 10,
    "input token sum",
  );
  assert.equal(
    sum("gen_ai.usage.output_tokens"),
    expected.outputTokens ?? expected.calls * 4,
    "output token sum",
  );
  if (!llms.length && expected.inputTokens)
    for (const r of roots)
      assert(
        attributes(r)["fi.usage.contribution"] === "additive" ||
          attributes(r)["fi.agent.usage.additive"] === true,
        "root usage must be additive",
      );
  for (const s of all) {
    const a = attributes(s);
    if (
      a["gen_ai.usage.total_tokens"] !== undefined &&
      a["gen_ai.usage.input_tokens"] !== undefined &&
      a["gen_ai.usage.output_tokens"] !== undefined
    )
      assert.equal(
        Number(a["gen_ai.usage.total_tokens"]),
        Number(a["gen_ai.usage.input_tokens"]) +
          Number(a["gen_ai.usage.output_tokens"]),
        "total token counter",
      );
  }
  if (llms.length)
    for (const r of roots)
      assert(
        !attributes(r)["gen_ai.usage.input_tokens"] &&
          !attributes(r)["gen_ai.usage.output_tokens"],
        "usage present on both root and generations",
      );
  // A conserved total is not enough: moving one request's tokens to another
  // generation corrupts attribution. Source aggregate-only drivers explicitly
  // declare last-generation ownership; all other drivers carry request usage.
  for (const root of roots) {
    const generations = llms.filter(s => s.traceId === root.traceId).sort((a, b) =>
      BigInt(a.startTimeUnixNano || 0) < BigInt(b.startTimeUnixNano || 0) ? -1 :
      BigInt(a.startTimeUnixNano || 0) > BigInt(b.startTimeUnixNano || 0) ? 1 : 0);
    for (const [i, s] of generations.entries()) {
      const aggregate = expected.generationUsage === "last-generation";
      for (const [key, total] of [
        ["gen_ai.usage.input_tokens", expected.inputTokens ?? expected.calls * 10],
        ["gen_ai.usage.output_tokens", expected.outputTokens ?? expected.calls * 4],
      ]) {
        let value = aggregate ? (i === generations.length - 1 ? total / roots.length : 0) : total / llms.length;
        if (expected.requestUsage) {
          const model = attributes(s)["gen_ai.request.model"] || attributes(s)["gen_ai.response.model"];
          const request = expected.requestIdentity === "order" ? expected.requestUsage[i] : expected.requestUsage.find(r => r.model === model);
          assert(request, "unrecognized generation request model");
          if (expected.requestIdentity !== "order") assert.equal(generations.filter(g => (attributes(g)["gen_ai.request.model"] || attributes(g)["gen_ai.response.model"]) === model).length, 1, "duplicate request model");
          value = key.endsWith("input_tokens") ? request.input : request.output;
        }
        assert.equal(Number(attributes(s)[key] || 0), value, `per-generation ${key} (${s.name})`);
      }
    }
  }
  if (roots.length > 1) {
    assert.equal(
      new Set(roots.map((r) => attributes(r)["session.id"])).size,
      1,
      "session grouping",
    );
    assert.equal(
      new Set(roots.map((r) => r.traceId)).size,
      roots.length,
      "one trace per prompt",
    );
  }
}
function checkPrivacy(batches, policy, expected) {
  if (expected) checkShape(batches, expected);
  const text = JSON.stringify(batches);
  assert(spans(batches).length, "privacy check requires real exported spans");
  const missing =
    policy === "off"
      ? Object.values(MARKERS)
      : policy.includes("INPUT")
        ? [MARKERS.input, MARKERS.secret]
        : policy.includes("OUTPUT")
          ? [MARKERS.output, MARKERS.tool, MARKERS.secret]
          : policy.includes("ERROR")
            ? [MARKERS.error, MARKERS.secret]
            : [MARKERS.secret];
  for (const value of [...missing, "synthetic-secret", "SYNTH_PASSWORD_19275"])
    assert(
      !text.includes(value),
      `content leak: ${value === MARKERS.secret ? "known-secret-shape" : value}`,
    );
  if (policy === "error-visible")
    assert(
      text.includes(MARKERS.error),
      "fixture did not exercise error content",
    );
  if (policy === "default")
    for (const value of [MARKERS.input, MARKERS.output, MARKERS.tool])
      assert(text.includes(value), `default content missing ${value}`);
}
/** Drivers feed native payloads only; assertions always inspect HTTP OTLP and the real CLI. */
async function runConformance(driver) {
  const checks = [];
  const evidenceDir = process.env.CONFORMANCE_EVIDENCE;
  async function record(name, fn) {
    const started = Date.now();
    try {
      await fn();
      checks.push({ check: name, status: "PASS", ms: Date.now() - started });
    } catch (e) {
      checks.push({
        check: name,
        status: "FAIL",
        evidence: String(e.message).slice(0, 900),
        ms: Date.now() - started,
      });
    }
  }
  const scenario = (more = {}) => ({
    id: "turn-1",
    session: "conf-session",
    calls: 2,
    tools: 2,
    model: "conf-model",
    outcome: "success",
    ...MARKERS,
    ...more,
  });
  async function traceCase(name, options = {}, policy) {
    await record(name, async () => {
      const f = await fixture(
        driver.captureAgent ?? driver.agent,
        policy && !["default", "error-visible"].includes(policy)
          ? {
              [policy === "off" ? "FI_AGENT_CAPTURE_CONTENT" : policy]:
                policy === "off" ? "false" : "true",
            }
          : {},
        driver.decodeRequest,
      );
      try {
        const d = await driver.create(f);
        if (d.close) f.cleanup.push(() => d.close());
        const s = scenario(options);
        await d.turn(s);
        if (d.flush) await d.flush();
        const expected = {
          ...s,
          inputTokens: s.inputTokens ?? s.calls * 10,
          outputTokens: s.outputTokens ?? s.calls * 4,
          generationUsage: driver.generationUsage,
          requestIdentity: driver.requestIdentity,
          childAgents: (s.aggregate ? driver.aggregateChildAgents : undefined) ?? driver.childAgents ?? 0,
          toolOutcome: s.outcome === "error" ? "error" : "success",
          // Cursor cannot read transcripts when content capture is disabled.
          // Its native turn totals remain additive on the root, never lost.
          ...(driver.rootFallbackPolicies?.includes(policy) ? { calls: 0 } : {}),
        };
        if (policy) checkPrivacy(f.batches, policy, expected);
        else checkShape(f.batches, expected);
        if (name === "privacy.off") await record("guard.privacy-dropped-spans-and-usage", async () => {
          const missing = structuredClone(f.batches);
          for (const b of missing) for (const r of b.resourceSpans || []) for (const scope of r.scopeSpans || [])
            scope.spans = scope.spans.filter(x => !x.parentSpanId);
          assert.throws(() => checkPrivacy(missing, policy, expected));
          if (expected.childAgents) {
            const noAgents = structuredClone(f.batches);
            const all = spans(noAgents);
            const agents = all.filter(x => x.parentSpanId && (attributes(x)["fi.span.kind"] ?? attributes(x)["gen_ai.span.kind"]) === "AGENT");
            // Native parents/children can arrive in different OTLP scopes/batches.
            // Preserve topology globally so this mutation isolates the count guard.
            for (const agent of agents)
              for (const span of all)
                if (span.traceId === agent.traceId && span.parentSpanId === agent.spanId) span.parentSpanId = agent.parentSpanId;
            for (const b of noAgents) for (const r of b.resourceSpans || []) for (const scope of r.scopeSpans || [])
              scope.spans = scope.spans.filter(x => !agents.includes(x));
            assert.throws(() => checkPrivacy(noAgents, policy, expected), /non-root agent spans/);
          }
          const noUsage = structuredClone(f.batches);
          for (const span of spans(noUsage)) span.attributes = (span.attributes || []).filter(a => !a.key.startsWith("gen_ai.usage."));
          assert.throws(() => checkPrivacy(noUsage, policy, expected));
        });
        if (name === "outcome.error") await record("guard.failed-tool-as-success", async () => {
          const wrong = structuredClone(f.batches);
          const tool = spans(wrong).find(x => (attributes(x)["fi.span.kind"] ?? attributes(x)["gen_ai.span.kind"]) === "TOOL");
          assert(tool, "missing failed-tool fixture");
          tool.status = {code:1};
          tool.attributes = tool.attributes.filter(a => a.key !== "fi.agent.outcome");
          tool.attributes.push({key:"fi.agent.outcome",value:{stringValue:"success"}});
          assert.throws(() => checkShape(wrong, expected), /tool outcome|tool status/);
        });
        if (name === "trace.shape-and-exact-usage") await record("guard.misattributed-generation-usage", async () => {
          const wrong = structuredClone(f.batches);
          const llms = spans(wrong).filter(x => (attributes(x)["fi.span.kind"] ?? attributes(x)["gen_ai.span.kind"]) === "LLM");
          assert.equal(llms.length, 2);
          const key = "gen_ai.usage.input_tokens";
          const total = llms.reduce((n,x) => n + Number(attributes(x)[key] || 0),0);
          // Move all usage to a different generation while keeping its sum exact.
          const owner = llms.findIndex(x => Number(attributes(x)[key] || 0) > 0);
          for (const [i,span] of llms.entries()) {
            span.attributes = span.attributes.filter(a => a.key !== key && a.key !== "gen_ai.usage.total_tokens");
            span.attributes.push({key,value:{intValue:i === 1-owner ? total : 0}});
          }
          assert.throws(() => checkShape(wrong, expected), /per-generation/);
        });
      } finally {
        if (evidenceDir) {
          const dir = path.join(evidenceDir, driver.agent);
          await fs.mkdir(dir, { recursive: true });
          await fs.writeFile(
            path.join(dir, name + ".json"),
            JSON.stringify(
              { otlp: f.batches, requests: f.requests.length },
              null,
              2,
            ),
          );
        }
        await f.close();
      }
    });
  }
  await traceCase("trace.shape-and-exact-usage");
  if (!driver.generationUsage)
    await traceCase("usage.distinct-request-attribution", {
      requestUsage: [
        { model: "conf-model-first", input: 11, output: 3 },
        { model: "conf-model-second", input: 29, output: 7 },
      ], inputTokens: 40, outputTokens: 10,
    });
  // Only hosts that supply native turn-level totals can exercise this fallback;
  // request-only adapters must never invent an aggregate counter.
  if (driver.aggregateUsage)
    await traceCase("usage.root-fallback-without-generations", {
      calls: 0,
      tools: 0,
      aggregate: true,
      inputTokens: 20,
      outputTokens: 8,
    });
  await record("trace.session-grouping", async () => {
    const f = await fixture(
      driver.captureAgent ?? driver.agent,
      {},
      driver.decodeRequest,
    );
    try {
      const d = await driver.create(f);
      if (d.close) f.cleanup.push(() => d.close());
      await d.turn(scenario());
      await d.turn(
        scenario({ id: "turn-2", input: MARKERS.input + " second" }),
      );
      if (d.flush) await d.flush();
      checkShape(f.batches, { turns: 2, calls: 4, tools: 4, generationUsage: driver.generationUsage, toolOutcome: "success", childAgents: (driver.childAgents ?? 0) * 2 });
    } finally {
      await f.close();
    }
  });
  for (const outcome of ["error", "interrupted", "incomplete"])
    await traceCase("outcome." + outcome, {
      outcome,
      ...(outcome === "incomplete" && driver.incompleteUsageUnavailable
        ? { inputTokens: 0, outputTokens: 0 }
        : {}),
    });
  await traceCase("scale.60-tools-60-model-calls", { calls: 60, tools: 60 });
  await traceCase("timing.overlapping-hooks", {
    calls: 8,
    tools: 8,
    overlap: true,
  });
  await traceCase("timing.terminal-record-six-seconds-late", { lateMs: 6000 });
  await traceCase(
    "privacy.error-baseline-visible",
    { outcome: "error" },
    "error-visible",
  );
  for (const policy of ["default", "off", ...FLAGS])
    await traceCase(
      "privacy." + policy,
      policy === "FI_HIDE_ERROR_DETAILS" ? { outcome: "error" } : {},
      policy,
    );
  // CLI tests are independent: an unsupported host must not hide key/config/scope regressions.
  const f = await fixture(driver.agent, {}, driver.decodeRequest);
  try {
    if (driver.cli) f.cli = (args, extra) => driver.cli(f, args, extra);
    const secrets = {
      FI_API_KEY: "synthetic-api",
      FI_SECRET_KEY: "synthetic-secret",
      FI_OTLP_ENDPOINT: f.endpoint,
    };
    await record("lifecycle.setup-validates-keys", async () => {
      f.setAuthFailure(true);
      try {
        const r = await f.cli(
          [
            "setup",
            driver.agent,
            "--configure-only",
            "--project",
            "conformance",
            "--json",
          ],
          secrets,
        );
        assert.notEqual(r.code, 0);
        assert.match(r.stdout + r.stderr, /AUTH|401|credential|key/i);
      } finally {
        f.setAuthFailure(false);
      }
    });
    await record("lifecycle.setup-private-config", async () => {
      const r = await f.cli(
        [
          "setup",
          driver.agent,
          "--configure-only",
          "--project",
          "conformance",
          "--json",
        ],
        secrets,
      );
      assert.equal(r.code, 0, r.stdout + r.stderr);
      assert(
        f.requests.some(
          (r) => r.body.resourceSpans?.length === 0 && r.authMatches,
        ),
        "setup did not validate the configured credential pair",
      );
      for (const file of [
        f.config.paths.user,
        path.join(f.config.paths.configDir, "credentials.json"),
      ])
        assert.equal((await fs.stat(file)).mode & 0o777, 0o600, file);
      assert(
        !(r.stdout + r.stderr).includes("synthetic-secret"),
        "CLI exposes key",
      );
    });
    await record("lifecycle.status-doctor-actionable", async () => {
      for (const cmd of ["status", "doctor"]) {
        const r = await f.cli([cmd, driver.agent, "--json"]);
        assert(!r.timedOut, `${cmd} timeout`);
        assert(r.stdout.trim(), `${cmd} empty output`);
        const value = JSON.parse(r.stdout);
        if (r.code !== 0)
          assert(
            /fix|next|setup|install|run|restart|reload|trust|missing|not_found|required/i.test(
              JSON.stringify(value),
            ),
            `${cmd} lacks remediation: ${r.stdout}`,
          );
      }
    });
    await record("lifecycle.healthy-status-doctor-zero", async () => {
      if (!driver.prepareHost)
        throw Error(
          "COVERAGE_GAP: driver has no healthy native host registration fixture; configure-only is not a healthy install",
        );
      await driver.prepareHost(f);
      const setup = await f.cli(
        ["setup", driver.agent, "--project", "conformance", "--json"],
        secrets,
      );
      assert.equal(setup.code, 0, setup.stdout + setup.stderr);
      f.config = await core.resolveConfig(f.options);
      const d = await driver.create(f);
      try {
        await d.turn(scenario({ id: "activation-turn" }));
        if (d.flush) await d.flush();
      } finally {
        if (d.close) await d.close();
      }
      assert(spans(f.batches).length, "healthy fixture did not export a turn");
      for (const cmd of ["status", "doctor"]) {
        const r = await f.cli([cmd, driver.agent, "--json"]);
        assert.equal(r.code, 0, `${cmd}: ${r.stdout}${r.stderr}`);
      }
    });
    await record("lifecycle.user-only-project-scope", async () => {
      const projectSetup = await f.cli(
        [
          "setup",
          driver.agent,
          "--scope",
          "project",
          "--configure-only",
          "--project",
          "scope-project",
          "--json",
        ],
        secrets,
      );
      assert.equal(
        projectSetup.code,
        0,
        projectSetup.stdout + projectSetup.stderr,
      );
      const local = await f.cli(["status", driver.agent, "--json"]);
      const user = await f.cli([
        "status",
        driver.agent,
        "--user-only",
        "--json",
      ]);
      assert.equal(JSON.parse(local.stdout).projectName, "scope-project");
      assert.equal(JSON.parse(user.stdout).projectName, "conformance");
    });
    await record("lifecycle.disable-stops-export", async () => {
      f.config = await core.resolveConfig(f.options);
      const d = await driver.create(f);
      try {
        const disabled = await f.cli(["disable", driver.agent, "--json"]);
        assert.equal(disabled.code, 0, disabled.stdout + disabled.stderr);
        const before = spans(f.batches).length;
        await d.turn(scenario({ id: "disabled-turn" }));
        if (d.flush) await d.flush();
        assert.equal(spans(f.batches).length, before, "export after disable");
      } finally {
        if (d.close) await d.close();
      }
    });
    await record("lifecycle.uninstall-preserves-other-agent", async () => {
      const other = driver.agent === "pi" ? "codex" : "pi";
      await core.setup({
        ...f.options,
        agent: other,
        configureOnly: true,
        projectName: "other-agent",
        otlpEndpoint: f.endpoint,
        credentials: {
          apiKey: "other-synthetic-api",
          secretKey: "other-synthetic-secret",
        },
      });
      const otherConfig = await core.resolveConfig({
        ...f.options,
        agent: other,
      });
      const before = await fs.readFile(otherConfig.paths.user, "utf8");
      const r = await f.cli(["uninstall", driver.agent, "--json"]);
      assert.equal(r.code, 0, r.stdout + r.stderr);
      assert.equal(await fs.readFile(otherConfig.paths.user, "utf8"), before);
      if (f.foreignSettings)
        assert.equal(
          JSON.parse(await fs.readFile(f.foreignSettings, "utf8"))
            .conformanceForeignSetting,
          "must-survive",
          "uninstall changed foreign native settings",
        );
      const restored = await core.resolveConfig({ ...f.options, agent: other });
      assert.equal(restored.projectName, "other-agent");
      assert.equal(restored.credentials.secretKey, "other-synthetic-secret");
    });
  } finally {
    await f.close();
  }
  return {
    group: driver.group || driver.agent,
    adapter: driver.agent,
    captureAgent: driver.captureAgent ?? driver.agent,
    checks,
    failures: checks
      .filter((c) => c.status === "FAIL")
      .map((c) => `${c.check}: ${c.evidence}`),
  };
}
module.exports = {
  runConformance,
  checkShape,
  checkPrivacy,
  fixture,
  attributes,
  spans,
  run,
  delay,
  MARKERS,
  FLAGS,
  packages,
  requestUsage: (scenario, index) => scenario.requestUsage?.[index] ?? { model: scenario.model, input: 10, output: 4 },
};
