// E2B only. Real OMP/Bun + packed packages + loopback model/OTLP/npm registry.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const exec = promisify(execFile);
const base = "/work/omp-acceptance";
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
async function main() {
  await fs.mkdir(base, { recursive: true });
  const distributions = {};
  for (const file of await fs.readdir("/work/packs")) {
    const absolute = path.join("/work/packs", file);
    const meta = JSON.parse(
      (await exec("tar", ["-xOf", absolute, "package/package.json"])).stdout,
    );
    distributions[meta.name] = { file, absolute, meta };
  }
  // Exercise a real native upgrade from an older setup-owned distribution.
  const pluginName = "@traceai/oh-my-pi-plugin";
  const oldRoot = path.join(base, "old-package");
  await fs.mkdir(oldRoot);
  await exec("tar", ["-xzf", distributions[pluginName].absolute, "-C", oldRoot]);
  const oldMeta = { ...distributions[pluginName].meta, version: "0.0.9" };
  await fs.writeFile(path.join(oldRoot, "package/package.json"), JSON.stringify(oldMeta));
  const oldFile = "traceai-oh-my-pi-plugin-0.0.9.tgz";
  await exec("tar", ["-czf", path.join("/work/packs", oldFile), "-C", oldRoot, "package"]);
  const batches = [],
    requests = [],
    runs = [];
  let retry = false;
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, "http://localhost");
      if (url.pathname.startsWith("/packs/")) {
        res.end(
          await fs.readFile(
            path.join("/work/packs", path.basename(url.pathname)),
          ),
        );
        return;
      }
      if (url.pathname === "/tracer/v1/traces") {
        let text = "";
        for await (const b of req) text += b;
        assert.equal(req.headers["x-api-key"], "dummy-fi-api");
        assert.equal(req.headers["x-secret-key"], "dummy-fi-secret");
        assert.equal(req.headers["x-project-name"], "coding-agents");
        batches.push(JSON.parse(text));
        res.setHeader("content-type", "application/json");
        res.end("{}");
        return;
      }
      if (url.pathname === "/v1/chat/completions") {
        let text = "";
        for await (const b of req) text += b;
        const body = JSON.parse(text);
        requests.push(body);
        assert.equal(body.model, "test");
        assert.equal(req.headers.authorization, "Bearer dummy-model");
        const last = body.messages.at(-1),
          user = [...body.messages].reverse().find((m) => m.role === "user");
        const prompt =
          typeof user?.content === "string"
            ? user.content
            : JSON.stringify(user?.content);
        if (prompt.includes("QUEUE_START"))
          await new Promise((r) => setTimeout(r, 1500));
        if (prompt.includes("RETRY_ONCE") && !retry) {
          retry = true;
          res.writeHead(429, {
            "content-type": "application/json",
            "retry-after": "0",
          });
          res.end(
            JSON.stringify({
              error: { message: "synthetic retry", type: "rate_limit_error" },
            }),
          );
          return;
        }
        const isAdvisor = body.tools?.some(
          (t) => t.function?.name === "advise",
        );
        if (isAdvisor) await new Promise((r) => setTimeout(r, 300));
        let tool;
        if (!isAdvisor && last.role !== "tool" && prompt.includes("TOOL_LOOP"))
          tool = { name: "read", args: { path: "sample.txt" } };
        if (last.role !== "tool" && prompt.includes("TOOL_FAILURE"))
          tool = { name: "read", args: { path: "missing.txt" } };
        const taskTool = body.tools?.find((t) => t.function?.name === "task");
        const isChild = body.tools?.some((t) => t.function?.name === "yield");
        if (isChild) {
          tool =
            last.role === "tool"
              ? { name: "yield", args: { data: "Synthetic child result" } }
              : { name: "read", args: { path: "sample.txt" } };
        } else if (
          last.role !== "tool" &&
          prompt.includes("PARENT_SUBAGENT") &&
          taskTool
        ) {
          const item = {
            agent: "task",
            task: "CHILD_WORK: read sample.txt and yield the result.",
            solutionSpace: "Use only the local sample file.",
          };
          tool = {
            name: "task",
            args: taskTool.function.parameters?.properties?.tasks
              ? { context: "Synthetic isolated tracing test.", tasks: [item] }
              : item,
          };
        }
        if (
          !tool &&
          last.role !== "tool" &&
          prompt.includes("CHILD_WORK") &&
          body.tools?.some((t) => t.function?.name === "read")
        )
          tool = { name: "read", args: { path: "sample.txt" } };
        if (tool && !body.tools?.some((t) => t.function?.name === tool.name))
          throw Error("mock requested unavailable tool");
        res.writeHead(200, { "content-type": "text/event-stream" });
        const send = (x) =>
          res.write(
            `data: ${JSON.stringify({ id: `mock-${requests.length}`, object: "chat.completion.chunk", created: 1, model: "test", ...x })}\n\n`,
          );
        send({
          choices: [
            {
              index: 0,
              delta: { role: "assistant", content: "" },
              finish_reason: null,
            },
          ],
        });
        if (tool)
          send({
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index: 0,
                      id: `call-${requests.length}`,
                      type: "function",
                      function: {
                        name: tool.name,
                        arguments: JSON.stringify(tool.args),
                      },
                    },
                  ],
                },
                finish_reason: null,
              },
            ],
          });
        else
          send({
            choices: [
              {
                index: 0,
                delta: {
                  content: isAdvisor
                    ? "ADVISOR_ONLY_OUTPUT"
                    : "Synthetic OMP answer.",
                },
                finish_reason: null,
              },
            ],
          });
        send({
          choices: [
            {
              index: 0,
              delta: {},
              finish_reason: tool ? "tool_calls" : "stop",
            },
          ],
          usage: {
            prompt_tokens: 40,
            completion_tokens: 10,
            total_tokens: 50,
            prompt_tokens_details: { cached_tokens: 5 },
          },
        });
        res.end("data: [DONE]\n\n");
        return;
      }
      const name = decodeURIComponent(url.pathname.slice(1)),
        p = distributions[name];
      if (p) {
        const origin = `http://127.0.0.1:${server.address().port}`;
        res.setHeader("content-type", "application/json");
        res.end(
          JSON.stringify({
            name,
            "dist-tags": { latest: p.meta.version },
            versions: {
              ...(name === pluginName ? { [oldMeta.version]: { ...oldMeta, dist: { tarball: `${origin}/packs/${oldFile}` } } } : {}),
              [p.meta.version]: {
                ...p.meta,
                dist: { tarball: `${origin}/packs/${p.file}` },
              },
            },
          }),
        );
        return;
      }
      res.writeHead(302, { location: `https://registry.npmjs.org${req.url}` });
      res.end();
    } catch (e) {
      console.error("mock failure", e.message);
      res.writeHead(500);
      res.end("{}");
    }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const home = path.join(base, "home"),
      agentDir = path.join(home, ".omp/agent"),
      cwd = path.join(base, "project");
    await fs.mkdir(agentDir, { recursive: true });
    await fs.mkdir(cwd, { recursive: true });
    await fs.writeFile(
      path.join(cwd, "sample.txt"),
      "Synthetic repository file.",
    );
    const env = {
      ...process.env,
      HOME: home,
      PI_CODING_AGENT_DIR: agentDir,
      FI_AGENT_CONFIG_DIR: path.join(home, "fi"),
      XDG_STATE_HOME: path.join(home, "state"),
      MOCK_API_KEY: "dummy-model",
      OTEL_SDK_DISABLED: "true",
      npm_config_registry: origin,
      BUN_CONFIG_VERBOSE_FETCH: "false",
      npm_config_cache: path.join(home, "npm-cache"),
      NO_COLOR: "1",
    };
    await fs.writeFile(
      path.join(home, ".npmrc"),
      `@traceai:registry=${origin}\n`,
    );
    await fs.writeFile(
      path.join(home, "bunfig.toml"),
      `[install.scopes]\n"@traceai" = "${origin}"\n`,
    );
    await fs.writeFile(
      path.join(agentDir, "models.yml"),
      JSON.stringify({
        providers: {
          mock: {
            baseUrl: `${origin}/v1`,
            api: "openai-completions",
            apiKey: "MOCK_API_KEY",
            models: [
              {
                id: "test",
                name: "test",
                reasoning: false,
                input: ["text"],
                cost: { input: 1, output: 2, cacheRead: 0.1, cacheWrite: 1 },
                contextWindow: 100000,
                maxTokens: 1024,
              },
            ],
          },
        },
      }),
    );
    await fs.writeFile(
      path.join(agentDir, "config.yml"),
      JSON.stringify({
        compaction: { enabled: false },
        telemetry: { otlpExportEnabled: false },
        update: { checkOnStartup: false },
        modelRoles: {
          default: "mock/test",
          smol: "mock/test",
          slow: "mock/test",
          task: "mock/test",
          plan: "mock/test",
        },
        async: { enabled: false },
        task: { batch: true, prewalk: false, isolation: { enabled: false } },
      }),
    );
    const version = (
      await exec("omp", ["--version"], { env, cwd })
    ).stdout.trim();
    assert.equal(version, "omp/18.17.0");
    const cliInstall = path.join(base, "cli");
    await fs.mkdir(cliInstall);
    await exec(
      "npm",
      [
        "install",
        "--prefix",
        cliInstall,
        "--legacy-peer-deps",
        "--no-audit",
        "--no-fund",
        ...Object.values(distributions).map((p) => p.absolute),
      ],
      { env, timeout: 120000, maxBuffer: 4e6 },
    );
    const cli = path.join(
      cliInstall,
      "node_modules/@traceai/agents/dist/cli.js",
    );
    const setupEnv = {
      ...env,
      FI_API_KEY: "dummy-fi-api",
      FI_SECRET_KEY: "dummy-fi-secret",
      FI_OTLP_ENDPOINT: `${origin}/tracer/v1/traces`,
    };
    const command = async (args, e = env) => {
      const r = exec(process.execPath, [cli, ...args], {
        env: e,
        cwd,
        timeout: 120000,
        maxBuffer: 2e6,
      });
      r.child.stdin.end();
      try {
        return await r;
      } catch (err) {
        if (args[0] === "setup") {
          for (const a of [
            ["plugin", "list", "--json"],
            ["install", "npm:@traceai/oh-my-pi-plugin@0.1.0", "--json"],
          ]) {
            try {
              console.log(
                "native diagnostic",
                a,
                (
                  await exec("omp", a, {
                    env,
                    cwd,
                    timeout: 120000,
                    maxBuffer: 2e6,
                  })
                ).stdout,
              );
            } catch (native) {
              console.log("native diagnostic", a, native.stdout, native.stderr);
            }
          }
        }
        throw err;
      }
    };
    const setup = await command(
      ["setup", "oh-my-pi", "--project", "coding-agents", "--json"],
      setupEnv,
    );
    console.log("setup", setup.stdout);
    assert.equal(JSON.parse(setup.stdout.trim()).registered, true);
    const repeat = await command(
      ["setup", "oh-my-pi", "--project", "coding-agents", "--json"],
      setupEnv,
    );
    assert.equal(JSON.parse(repeat.stdout.trim()).registered, true);
    await exec("omp", ["install", `npm:${pluginName}@0.0.9`, "--json"], { env, cwd, timeout: 120000 });
    const older = JSON.parse((await exec("omp", ["plugin", "list", "--json"], { env, cwd })).stdout).npm.find((p) => p.name === pluginName);
    assert.equal(older.version, "0.0.9");
    const core = require("@traceai/agent-core");
    await core.updateJson(path.join(home, "fi/oh-my-pi-registration.json"), () => ({
      version: older.version, path: older.path, hash: core.contentHash(JSON.stringify(older)),
    }));
    const upgraded = await command(["update", "oh-my-pi", "--json"]);
    assert.equal(JSON.parse(upgraded.stdout).registered, true);
    const refreshedOwner = await core.readJson(path.join(home, "fi/oh-my-pi-registration.json"));
    assert.equal(refreshedOwner.version, "0.1.0");
    console.log("OWNED_NATIVE_UPGRADE_OK");
    const native = JSON.parse(
      (await exec("omp", ["plugin", "list", "--json"], { env, cwd })).stdout,
    );
    assert.equal(
      native.npm.filter((p) => p.name === "@traceai/oh-my-pi-plugin").length,
      1,
    );
    assert.equal(
      (await fs.stat(path.join(home, "fi/credentials.json"))).mode & 0o777,
      0o600,
    );
    const spans = () =>
      batches.flatMap((b) =>
        b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
      );
    async function run(prompt, extra = [], changes = {}) {
      const before = spans().length;
      const job = exec(
        "omp",
        [
          "--print",
          "--no-title",
          "--model",
          "mock/test",
          "--tools",
          "read",
          ...extra,
          prompt,
        ],
        { env: { ...env, ...changes }, cwd, timeout: 65000, maxBuffer: 5e6 },
      );
      job.child.stdin.end();
      let r;
      try {
        r = await job;
      } catch (e) {
        await fs.writeFile(
          path.join(base, "failure.json"),
          JSON.stringify(
            { stdout: e.stdout, stderr: e.stderr, requests },
            null,
            2,
          ),
        );
        throw e;
      }
      const received = spans().slice(before);
      runs.push({
        prompt,
        stdout: r.stdout,
        stderr: r.stderr,
        spans: received.map((s) => ({
          name: s.name,
          traceId: s.traceId,
          spanId: s.spanId,
          parentSpanId: s.parentSpanId,
          attributes: attrs(s),
        })),
      });
      await fs.writeFile(
        path.join(base, "evidence.json"),
        JSON.stringify(
          {
            version,
            bun: "1.4.4",
            native,
            runs,
            requestCount: requests.length,
          },
          null,
          2,
        ),
      );
      console.log(
        "run",
        prompt,
        "spans",
        received.map((s) => s.name),
      );
      return received;
    }
    const basic = await run("PRINT_HELLO");
    assert.equal(basic.filter((s) => !s.parentSpanId).length, 1);
    assert(basic.some((s) => attrs(s)["fi.span.kind"] === "LLM"));
    const tools = await run("TOOL_LOOP");
    assert.equal(tools.filter((s) => !s.parentSpanId).length, 1);
    assert(tools.some((s) => attrs(s)["fi.span.kind"] === "TOOL"));
    assert.equal(
      tools.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      2,
    );
    const failed = await run("TOOL_FAILURE");
    assert(
      failed.some(
        (s) => attrs(s)["fi.span.kind"] === "TOOL" && s.status.code === 2,
      ),
    );
    const retried = await run("RETRY_ONCE");
    assert(retried.some((s) => attrs(s)["fi.span.kind"] === "LLM"));
    const sub = await run("PARENT_SUBAGENT", ["--tools", "read,task"]);
    assert(sub.some((s) => s.name === "task" && s.parentSpanId));
    assert(sub.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length >= 4);
    assert(
      sub.some(
        (s) =>
          s.name === "read" &&
          sub.some((p) => p.name === "task" && p.spanId === s.parentSpanId),
      ),
    );
    assert.equal(sub.filter((s) => !s.parentSpanId).length, 1);
    for (const s of sub.filter((s) => s.parentSpanId))
      assert(sub.some((p) => p.spanId === s.parentSpanId));
    const cfg = JSON.parse(
      await fs.readFile(path.join(agentDir, "config.yml"), "utf8"),
    );
    cfg.async = { enabled: true, printWake: true };
    await fs.writeFile(path.join(agentDir, "config.yml"), JSON.stringify(cfg));
    const bg = await run("PARENT_SUBAGENT_BACKGROUND", [
      "--tools",
      "read,task",
    ]);
    assert(bg.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length >= 3);
    assert.equal(bg.filter((s) => !s.parentSpanId).length, 1);
    cfg.async = { enabled: false };
    cfg.advisor = { enabled: true, syncBacklog: "strict" };
    cfg.modelRoles.advisor = "mock/test";
    await fs.writeFile(path.join(agentDir, "config.yml"), JSON.stringify(cfg));
    for (const prompt of ["ADVISOR_HELLO", "ADVISOR_TOOL_LOOP"]) {
      const requestStart = requests.length;
      const observed = await run(prompt, ["--advisor"]);
      const modelRequests = requests.slice(requestStart);
      const advisorRequests = modelRequests.filter((r) =>
        r.tools?.some((t) => t.function?.name === "advise"),
      );
      assert(
        advisorRequests.length,
        "must exercise the real advisor, not just a primary prompt",
      );
      const models = observed.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
      assert.equal(
        models.length,
        modelRequests.length - advisorRequests.length,
      );
      const root = observed.find((s) => !s.parentSpanId);
      assert.equal(attrs(root)["fi.agent.outcome"], "success");
      assert.equal(
        attrs(root)["fi.agent.gap"],
        "AUXILIARY_GENERATIONS_UNAVAILABLE",
      );
      assert(
        models.every(
          (s) => attrs(s)["fi.agent.fidelity"] === "reconstructed-message",
        ),
      );
      assert(
        models.every(
          (s) => attrs(s)["output.value"] && attrs(s)["input.value"],
        ),
      );
      assert(!JSON.stringify(observed).includes("ADVISOR_ONLY_OUTPUT"));
      assert.equal(
        observed.reduce(
          (sum, s) => sum + Number(attrs(s)["gen_ai.usage.total_tokens"] ?? 0),
          0,
        ),
        models.length * 50,
      );
      if (prompt.includes("TOOL_LOOP")) {
        assert.equal(models.length, 2);
        assert(observed.some((s) => attrs(s)["tool.name"] === "read"));
      }
    }
    cfg.advisor.enabled = false;
    await fs.writeFile(path.join(agentDir, "config.yml"), JSON.stringify(cfg));
    const policyExtension = path.join(base, "policy-retry.mjs");
    await fs.writeFile(
      policyExtension,
      `export default function(api) {
      let changed = false;
      api.on('before_agent_start', async (event) => {
        if (!event.prompt.includes('POLICY_RETRY')) return;
        console.error('POLICY_PREPARATION');
        if (!changed) { changed = true; await api.setActiveTools([]); }
        return { systemPrompt: [...event.systemPrompt, 'Synthetic prompt-time policy.'] };
      });
    }`,
    );
    const policyRequestStart = requests.length;
    const policy = await run("POLICY_RETRY", ["--extension", policyExtension]);
    assert(
      (runs.at(-1).stderr.match(/POLICY_PREPARATION/g) ?? []).length >= 2,
      "the real host must repeat prompt preparation",
    );
    assert.equal(requests.length - policyRequestStart, 1);
    assert.equal(policy.filter((s) => !s.parentSpanId).length, 1);
    assert.equal(
      policy.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      1,
    );
    assert.equal(
      attrs(policy.find((s) => !s.parentSpanId))["fi.agent.outcome"],
      "success",
    );
    await fs.writeFile(
      path.join(base, "policy-retry.json"),
      JSON.stringify(
        {
          requests: requests.length - policyRequestStart,
          preparations: (runs.at(-1).stderr.match(/POLICY_PREPARATION/g) ?? [])
            .length,
          roots: policy.filter((s) => !s.parentSpanId).length,
          generations: policy.filter((s) => attrs(s)["fi.span.kind"] === "LLM")
            .length,
        },
        null,
        2,
      ),
    );
    const privacy = await run("PRIVATE_MARKER", [], {
      FI_AGENT_CAPTURE_CONTENT: "false",
    });
    assert(privacy.length);
    assert(!JSON.stringify(privacy).includes("PRIVATE_MARKER"));
    assert(
      privacy.every(
        (s) =>
          attrs(s)["input.value"] === undefined &&
          attrs(s)["output.value"] === undefined,
      ),
    );
    await command(["disable", "oh-my-pi", "--json"]);
    const disabled = await run("DISABLED");
    assert.equal(disabled.length, 0);
    await command(["enable", "oh-my-pi", "--json"]);
    await run("RE_ENABLED_RECEIPT"); // Activation is scoped to the current disable/setup epoch.
    const status = await command(["status", "oh-my-pi", "--check", "--json"]);
    const report = JSON.parse(status.stdout.trim());
    assert.equal(report.registration, "REGISTERED");
    assert.equal(report.activation, "OBSERVED");
    assert.equal(report.credentialCheck, "ACCEPTED");
    await require("./protocols.cjs").runProtocols({ env, cwd, base, spans });
    console.log(
      JSON.stringify({
        status: "PASS",
        version,
        bun: "1.4.4",
        requests: requests.length,
        spans: spans().length,
        cases: runs.map((r) => r.prompt),
      }),
    );
  } finally {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  }
}
main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
