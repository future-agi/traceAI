const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
const core = require("@traceai/agent-core");
const { geminiAdapter, reconcile } = require("../dist/adapter.js");
const { applyEvent, usage, binding } = require("../dist/mapper.js");
const { seal, unseal } = require("../dist/runtime.js");
async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "gemini-adapter-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (d) => (raw += d));
    req.on("end", () => {
      requests.push({
        path: req.url,
        headers: req.headers,
        body: JSON.parse(raw),
      });
      res.setHeader("content-type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const env = {
    HOME: home,
    PATH: process.env.PATH,
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    FI_API_KEY: "test-fi-api",
    FI_SECRET_KEY: "test-fi-secret",
  };
  const options = { agent: "gemini", home, env, projectDir: home };
  await core.setup({ ...options, configureOnly: true });
  const config = await core.resolveConfig(options);
  const context = { config, scope: "user", projectDir: home };
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  return { home, env, options, config, context, requests };
}
function event(name, extra = {}, offset = 0) {
  return {
    hook_event_name: name,
    timestamp: new Date(Date.now() + offset).toISOString(),
    session_id: "public-session",
    ...extra,
  };
}
function attrs(span) {
  return Object.fromEntries(
    (span.attributes || []).map((a) => [a.key, Object.values(a.value)[0]]),
  );
}
function spans(f) {
  return f.requests.flatMap((r) =>
    r.body.resourceSpans.flatMap((rs) =>
      rs.scopeSpans.flatMap((ss) => ss.spans),
    ),
  );
}
async function hook(f, data, overrides = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [path.resolve(__dirname, "../dist/hook.cjs")],
      {
        cwd: f.home,
        env: { ...f.env, ...overrides },
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    let out = "",
      err = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", reject);
    child.on("close", (code) => {
      try {
        assert.equal(code, 0, err);
        assert.equal(out, "{}\n");
        assert.equal(err, "");
        resolve();
      } catch (e) {
        reject(e);
      }
    });
    child.stdin.end(typeof data === "string" ? data : JSON.stringify(data));
  });
}
test("unsafe workspace ancestors fall back to user configuration without losing the turn", async (t) => {
  const f = await fixture(t);
  const workspace = path.join(f.home, "external-volume-workspace");
  await fs.mkdir(workspace, { mode: 0o775 });
  await fs.chmod(workspace, 0o775);
  const runtimeFixture = { ...f, home: workspace };
  await hook(runtimeFixture, event("BeforeAgent", { prompt: "public external-volume prompt" }, 0));
  await hook(runtimeFixture, event("AfterAgent", { prompt_response: "public response" }, 1));
  assert.equal(spans(f).filter(span => !span.parentSpanId).length, 1);
  const status = await core.inspectStatus({ ...f.options, projectDir: workspace });
  assert.ok(status.issues.includes("PROJECT_CONFIG_IGNORED"));
});

test("registration preserves JSONC/unrelated hooks; repeat and uninstall are ownership-aware", async (t) => {
  const f = await fixture(t),
    settings = path.join(f.home, ".gemini", "settings.json");
  await fs.writeFile(
    settings,
    '{\n// keep this comment\n"ui":{"theme":"Dark"},"hooks":{"AfterTool":[/* preserve inline comment */ {"hooks":[{"name":"mine","type":"command","command":"true"}]}]}}',
    { mode: 0o644 },
  );
  assert.equal((await geminiAdapter.install(f.context)).registered, true);
  assert.equal((await geminiAdapter.install(f.context)).registered, true);
  let text = await fs.readFile(settings, "utf8");
  assert.match(text, /keep this comment/);
  assert.match(text, /"mine"/);
  assert.equal((text.match(/futureagi-gemini-AfterTool/g) || []).length, 1);
  assert.doesNotMatch(text, /test-fi-api|test-fi-secret|node_modules|_npx/);
  await geminiAdapter.uninstall(f.context);
  text = await fs.readFile(settings, "utf8");
  assert.match(text, /"mine"/);
  assert.match(text, /preserve inline comment/);
  assert.doesNotMatch(text, /futureagi-gemini-/);
});
test("native telemetry and disabled host hooks refuse registration without changing policy", async (t) => {
  const f = await fixture(t),
    settings = path.join(f.home, ".gemini", "settings.json");
  for (const body of [
    { telemetry: { enabled: true } },
    { hooksConfig: { enabled: false } },
  ]) {
    await fs.writeFile(settings, JSON.stringify(body), { mode: 0o600 });
    assert.equal((await geminiAdapter.install(f.context)).registered, false);
    assert.deepEqual(JSON.parse(await fs.readFile(settings, "utf8")), body);
  }
});
test("modified owned entries and symlinks are not overwritten", async (t) => {
  const f = await fixture(t),
    settings = path.join(f.home, ".gemini", "settings.json");
  await geminiAdapter.install(f.context);
  const text = await fs.readFile(settings, "utf8");
  await fs.writeFile(
    settings,
    text.replace(/"command": "[^"]+"/, '"command": "custom"'),
  );
  await assert.rejects(
    geminiAdapter.install(f.context),
    /REGISTRATION_CHANGED/,
  );
  await fs.unlink(settings);
  await fs.symlink(path.join(f.home, "target"), settings);
  await assert.rejects(geminiAdapter.install(f.context));
});
test("stream chunks own tokens once; synthetic BeforeModel alone never makes an LLM", async (t) => {
  const f = await fixture(t);
  await hook(f, event("BeforeAgent", { prompt: "public question" }, 1));
  await hook(
    f,
    event("BeforeModel", { llm_request: { model: "gemini-test" } }, 2),
  );
  for (let i = 0; i < 2; i++)
    await hook(
      f,
      event(
        "AfterModel",
        {
          llm_request: {
            model: "gemini-test",
            messages: [{ role: "user", content: "public input" }],
          },
          llm_response: {
            candidates: [
              {
                content: { parts: [i ? "world" : "hello "] },
                ...(i ? { finishReason: "STOP" } : {}),
              },
            ],
            usageMetadata: {
              promptTokenCount: 10,
              candidatesTokenCount: i ? 5 : 2,
              cachedContentTokenCount: 3,
              thoughtsTokenCount: 1,
            },
          },
        },
        3 + i,
      ),
    );
  await hook(
    f,
    event(
      "AfterTool",
      {
        tool_name: "read_file",
        tool_input: { path: "public.txt" },
        tool_response: { llmContent: "public tool result" },
      },
      5,
    ),
  );
  await hook(f, event("PreCompress", {}, 6));
  await hook(f, event("AfterAgent", { prompt_response: "public answer" }, 7));
  await hook(f, event("BeforeAgent", { prompt: "second question" }, 8));
  await hook(
    f,
    event("BeforeModel", { llm_request: { model: "synthetic" } }, 9),
  );
  await hook(
    f,
    event("AfterAgent", { prompt_response: "synthetic answer" }, 10),
  );
  const all = spans(f),
    roots = all.filter((s) => !s.parentSpanId),
    models = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(roots.length, 2);
  assert.equal(models.length, 1);
  assert.equal(attrs(roots[0])["session.id"], attrs(roots[1])["session.id"]);
  assert.equal(Number(attrs(models[0])["gen_ai.usage.input_tokens"]), 10);
  assert.equal(Number(attrs(models[0])["gen_ai.usage.output_tokens"]), 6);
  assert.equal(attrs(models[0])["output.value"], "hello world");
  assert.equal(all.filter((s) => s.name === "read_file").length, 1);
  assert.ok(
    all
      .filter((s) => s.parentSpanId)
      .every((s) => s.parentSpanId === roots[0].spanId),
  );
  assert.ok(
    roots.every((s) => attrs(s)["gen_ai.usage.input_tokens"] === undefined),
  );
  assert.ok(f.requests.every((r) => r.path === "/tracer/v1/traces"));
});
test("content privacy/error privacy, credential redaction, encrypted recovery and kill switch", async (t) => {
  const f = await fixture(t);
  await hook(
    f,
    event("BeforeAgent", { prompt: "PRIVATE test-fi-api test-fi-secret" }, 1),
  );
  const dir = path.join(f.config.paths.stateDir, "gemini-turns");
  const files = (await fs.readdir(dir)).filter((x) => x.endsWith(".json"));
  const raw = await fs.readFile(path.join(dir, files[0]), "utf8");
  assert.doesNotMatch(raw, /PRIVATE|test-fi-api|test-fi-secret/);
  assert.equal((await fs.stat(path.join(dir, files[0]))).mode & 0o777, 0o600);
  await hook(f, event("AfterAgent", { prompt_response: "answer" }, 2));
  assert.doesNotMatch(JSON.stringify(spans(f)), /test-fi-api|test-fi-secret/);
  await hook(f, event("BeforeAgent", { prompt: "SECRET PROMPT" }, 3), {
    FI_AGENT_CAPTURE_CONTENT: "false",
  });
  await hook(
    f,
    event(
      "AfterTool",
      {
        tool_name: "read_file",
        tool_response: {
          llmContent: "SECRET ERROR",
          error: { message: "SECRET ERROR" },
        },
      },
      4,
    ),
    { FI_AGENT_CAPTURE_CONTENT: "false" },
  );
  await hook(f, event("AfterAgent", { prompt_response: "SECRET OUTPUT" }, 5), {
    FI_AGENT_CAPTURE_CONTENT: "false",
  });
  assert.doesNotMatch(JSON.stringify(spans(f)), /SECRET/);
  const before = spans(f).length;
  await hook(f, event("BeforeAgent", { prompt: "disabled" }, 6));
  await core.disable(f.options);
  await hook(f, event("AfterAgent", { prompt_response: "disabled" }, 7));
  assert.equal(spans(f).length, before);
});
test("privacy or destination changes invalidate buffered content", async (t) => {
  const f = await fixture(t),
    state = applyEvent(
      undefined,
      event("BeforeAgent", { prompt: "private" }),
      f.config,
    );
  const raw = seal(state, f.config);
  assert.equal(unseal(raw, f.config).input, "private");
  assert.throws(() =>
    unseal(raw, {
      ...f.config,
      identityKey: f.config.identityKey,
      hideInputs: true,
    }),
  );
  assert.throws(() =>
    unseal(raw, {
      ...f.config,
      identityKey: f.config.identityKey,
      endpoint: "http://127.0.0.1:9/traces",
    }),
  );
});
test("respects Gemini home override and host-directory project policy", async (t) => {
  const f = await fixture(t);
  const p = await core.configPaths({
    ...f.options,
    env: { ...f.env, GEMINI_CLI_HOME: path.join(f.home, "alternate") },
  });
  assert.equal(
    p.user,
    path.join(f.home, "alternate", ".gemini", "futureagi.json"),
  );
  assert.equal(p.project, path.join(f.home, ".gemini", "futureagi.json"));
});
test("new native telemetry configuration fences recovery and export", async (t) => {
  const f = await fixture(t);
  await hook(f, event("BeforeAgent", { prompt: "PRIVATE" }, 1));
  await fs.writeFile(
    path.join(f.home, ".gemini", "settings.json"),
    JSON.stringify({ telemetry: { enabled: true } }),
    { mode: 0o600 },
  );
  await hook(f, event("AfterAgent", { prompt_response: "PRIVATE" }, 2));
  assert.equal(spans(f).length, 0);
  const status = JSON.parse(
    await fs.readFile(
      core.deliveryStatePath(f.config),
      "utf8",
    ),
  );
  assert.equal(status.lastError, "NATIVE_TELEMETRY_CONFLICT");
});
test("incomplete terminal boundaries, repeated stop and explicit tool errors are truthful", async (t) => {
  const f = await fixture(t);
  await hook(f, event("BeforeAgent", { prompt: "public" }, 1));
  await hook(
    f,
    event(
      "AfterTool",
      {
        tool_name: "read_file",
        tool_input: { path: "missing" },
        tool_response: {
          llmContent: "PRIVATE ERROR",
          error: { message: "PRIVATE ERROR" },
        },
      },
      2,
    ),
    { FI_HIDE_ERROR_DETAILS: "true" },
  );
  // A changed privacy setting discards old content; start a fresh turn under the same stricter policy.
  await hook(f, event("BeforeAgent", { prompt: "public" }, 3), {
    FI_HIDE_ERROR_DETAILS: "true",
  });
  await hook(
    f,
    event(
      "AfterTool",
      {
        tool_name: "read_file",
        tool_response: {
          llmContent: "PRIVATE ERROR",
          error: { message: "PRIVATE ERROR" },
        },
      },
      4,
    ),
    { FI_HIDE_ERROR_DETAILS: "true" },
  );
  const stop = event("SessionEnd", {}, 5);
  await hook(f, stop, { FI_HIDE_ERROR_DETAILS: "true" });
  await hook(f, stop, { FI_HIDE_ERROR_DETAILS: "true" });
  const all = spans(f);
  assert.equal(all.filter((s) => !s.parentSpanId).length, 1);
  assert.equal(
    attrs(all.find((s) => !s.parentSpanId))["fi.agent.outcome"],
    "incomplete",
  );
  assert.equal(all.find((s) => s.name === "read_file").status.code, 2);
  assert.doesNotMatch(JSON.stringify(all), /PRIVATE ERROR/);
});
test("neutral response on malformed input and missing configuration", async (t) => {
  const f = await fixture(t);
  await hook(f, "{broken");
  await hook(f, event("AfterTool", { tool_response: { error: "ignored" } }));
  assert.equal(spans(f).length, 0);
});
