const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
const core = require("@traceai/agent-core");
const { RolloutReader } = require("../dist/rollout.js");
const { correlate } = require("../dist/ancestry.js");
const { EventStore } = require("../dist/state.js");
const { sanitizeEvent } = require("../dist/events.js");
const hook = path.resolve(__dirname, "../dist/hook.cjs");
const cli = path.resolve(__dirname, "../../traceai_agents/dist/cli.js");
const put = (p, v) => fs.writeFile(p, JSON.stringify(v), { mode: 0o600 });
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "codex-regression-"));
  const home = path.join(root, "home"),
    repo = path.join(root, "repo");
  const received = [];
  const auth = { rejected: new Set() };
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let bytes = Buffer.concat(chunks);
    if (req.headers["content-encoding"] === "gzip")
      bytes = require("node:zlib").gunzipSync(bytes);
    received.push({ headers: req.headers, body: JSON.parse(bytes.toString()) });
    res.writeHead(auth.rejected.has(req.headers["x-api-key"]) ? 401 : 200, {
      "content-type": "application/json",
    });
    res.end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(root, { recursive: true, force: true });
  });
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    CODEX_HOME: path.join(home, ".codex"),
    FI_AGENT_CONFIG_DIR: path.join(root, "config"),
    XDG_STATE_HOME: path.join(root, "state"),
  };
  for (const p of [
    env.CODEX_HOME,
    env.FI_AGENT_CONFIG_DIR,
    path.join(repo, ".codex"),
    path.join(env.CODEX_HOME, "sessions"),
  ])
    await fs.mkdir(p, { recursive: true, mode: 0o700 });
  const profile = {
    apiKey: "synthetic-fi-api",
    secretKey: "synthetic-fi-secret",
    endpoint,
    projectName: "test",
    validatedAt: new Date().toISOString(),
    captureContent: true,
    identityKey: "a".repeat(64),
    userId: "test-user",
  };
  const store = {
    schemaVersion: 1,
    defaultProfile: "test",
    profiles: { test: profile },
  };
  await put(path.join(env.FI_AGENT_CONFIG_DIR, "credentials.json"), store);
  await put(path.join(env.CODEX_HOME, "futureagi.json"), {
    schemaVersion: 1,
    enabled: true,
    profile: "test",
  });
  const options = { agent: "codex", home, env, projectDir: repo };
  const config = await core.resolveConfig(options);
  const run = (file, args = [], data, vars = env) =>
    new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [file, ...args], {
        env: vars,
        cwd: repo,
      });
      let out = "",
        err = "";
      child.stdout.on("data", (b) => (out += b));
      child.stderr.on("data", (b) => (err += b));
      child.on("error", reject);
      child.on("exit", (code) => resolve({ code, out, err }));
      child.stdin.end(data);
    });
  const invoke = async (name, rest = {}, vars) => {
    const result = await run(
      hook,
      [],
      JSON.stringify({
        hook_event_name: name,
        session_id: "s",
        turn_id: "t",
        cwd: repo,
        ...rest,
      }),
      vars,
    );
    assert.deepEqual(result, { code: 0, out: "", err: "" });
  };
  return {
    auth,
    root,
    home,
    repo,
    env,
    endpoint,
    received,
    profile,
    store,
    config,
    options,
    run,
    invoke,
  };
}
const row = (type, payload) =>
  JSON.stringify({ type, timestamp: new Date().toISOString(), payload }) + "\n";

test("ordinary status/doctor use hook cwd config; explicit project directory wins", async (t) => {
  const f = await fixture(t);
  await put(path.join(f.repo, ".codex/futureagi.json"), {
    schemaVersion: 1,
    enabled: false,
    captureContent: false,
    projectName: "repo-project",
    transport: { endpoint: f.endpoint + "/repo" },
  });
  const effective = await core.resolveConfig(f.options);
  for (const command of ["status", "doctor"]) {
    const result = await f.run(cli, [command, "codex", "--json"]);
    const status = JSON.parse(result.out);
    assert.equal(status.enabled, effective.enabled);
    assert.equal(status.privacy, "structure-only");
    assert.equal(status.destination, effective.endpoint);
    assert.equal(status.projectName, effective.projectName);
    assert(status.issues.includes("DISABLED"));
    assert(status.issues.includes("DESTINATION_NOT_APPROVED"));
    const explicit = JSON.parse(
      (await f.run(cli, [command, "codex", "--json", "--project-dir", f.home]))
        .out,
    );
    assert.equal(explicit.enabled, true);
    assert.equal(explicit.privacy, "content-on-by-default");
    assert.equal(explicit.destination, f.endpoint);
  }
});

test("environment credentials do not approve repository transport or another profile", async (t) => {
  const f = await fixture(t);
  f.profile.endpoint = "http://127.0.0.1:1/tracer/v1/traces";
  f.store.profiles.other = { ...f.profile, endpoint: f.endpoint };
  await put(f.config.paths.credentials, f.store);
  const env = {
    ...f.env,
    FI_API_KEY: "synthetic-env-api",
    FI_SECRET_KEY: "synthetic-env-secret",
  };
  for (const override of [
    { transport: { endpoint: f.endpoint } },
    { profile: "other" },
  ]) {
    await put(path.join(f.repo, ".codex/futureagi.json"), {
      schemaVersion: 1,
      ...override,
    });
    const cfg = await core.resolveConfig({ ...f.options, env });
    assert.equal(cfg.destinationApproved, false);
    assert.equal(cfg.enabled, false);
    await f.invoke("UserPromptSubmit", { prompt: "no egress" }, env);
    await f.invoke("Interrupt", {}, env);
    assert.equal(f.received.length, 0);
  }
  await put(path.join(f.repo, ".codex/futureagi.json"), {
    schemaVersion: 1,
    transport: { endpoint: f.endpoint },
  });
  assert.equal(
    (
      await core.resolveConfig({
        ...f.options,
        env: { ...env, FI_OTLP_ENDPOINT: f.endpoint },
      })
    ).destinationApproved,
    true,
  );
  f.profile.endpoint = f.endpoint;
  await put(f.config.paths.credentials, f.store);
  assert.equal(
    (await core.resolveConfig({ ...f.options, env })).destinationApproved,
    true,
    "saved destination binding remains valid",
  );
});

test("actual hook exports redact nested structured credentials and quoted JSON responses", async (t) => {
  const f = await fixture(t);
  const secrets = {
    token: "third-party-token-value",
    api_key: "third-party-api-value",
    password: "third-party-password-value",
    authorization: "Basic third-party-authorization-value",
  };
  await f.invoke("UserPromptSubmit", {
    prompt: "privacy fixture synthetic-fi-api synthetic-fi-secret",
  });
  await f.invoke("PreToolUse", {
    tool_use_id: "call",
    tool_name: "service",
    tool_input: { nested: [{ ...secrets, safe: "visible-tool-content" }] },
  });
  await f.invoke("PostToolUse", {
    tool_use_id: "call",
    tool_name: "service",
    tool_response:
      "Response: " +
      JSON.stringify({ nested: secrets, safe: "visible-output" }),
  });
  await f.invoke("Interrupt");
  assert(f.received.length > 0);
  const body = JSON.stringify(f.received.map((r) => r.body));
  for (const value of [
    ...Object.values(secrets),
    f.profile.apiKey,
    f.profile.secretKey,
  ])
    assert(!body.includes(value), value);
  assert(body.includes("visible-tool-content"));
  assert(body.includes("visible-output"));
  assert(/\[REDACTED\]|<redacted>/.test(body));
  const boundary = sanitizeEvent(
    {
      hook_event_name: "PostToolUse",
      session_id: "s",
      cwd: f.repo,
      tool_response: JSON.stringify({
        password: "long-private-value-".repeat(2000),
      }),
    },
    f.config,
  );
  assert(
    !boundary.tool_response.includes("long-private-value"),
    "truncate only after redacting quoted values",
  );
});

test("contended optional debug logging cannot suppress native hook exports", async (t) => {
  const f = await fixture(t);
  await fs.mkdir(f.config.paths.stateDir, { recursive: true, mode: 0o700 });
  await fs.mkdir(
    path.join(f.config.paths.stateDir, "codex-hook.json.write-lock"),
    { mode: 0o700 },
  );
  const env = { ...f.env, FI_AGENT_DEBUG: "true" };
  await f.invoke(
    "UserPromptSubmit",
    { prompt: "debug contention fixture" },
    env,
  );
  await f.invoke("Interrupt", {}, env);
  assert(f.received.length > 0);
  assert(
    JSON.stringify(f.received.map((r) => r.body)).includes(
      "debug contention fixture",
    ),
  );
});

test("large rollout makes bounded progress, checkpoints despite discarded history, and exports successive Stop turns", async (t) => {
  const f = await fixture(t),
    transcript = path.join(f.env.CODEX_HOME, "sessions/large.jsonl");
  const history = row("response_item", {
    type: "message",
    role: "assistant",
    content: [{ text: "h".repeat(9000) }],
  }).repeat(520);
  await fs.writeFile(
    transcript,
    row("session_meta", { id: "s" }) +
      history +
      row("event_msg", {
        type: "task_complete",
        turn_id: "t",
        last_agent_message: "large-done",
      }),
    { mode: 0o600 },
  );
  const reader = new RolloutReader(transcript, "s", f.config);
  await reader.read();
  assert(reader.hasMore);
  assert(!reader.completed.has("t"));
  await reader.read();
  assert(reader.completed.has("t"));
  assert(reader.gap, "historical retention bounded");
  await reader.checkpoint();
  const cursors = (await fs.readdir(f.config.paths.stateDir)).filter((n) =>
    n.startsWith("codex-cursor-"),
  );
  assert.equal(cursors.length, 1);
  assert.equal(
    JSON.parse(
      await fs.readFile(path.join(f.config.paths.stateDir, cursors[0])),
    ).offset,
    (await fs.stat(transcript)).size,
  );
  // Start from an uncaptured large existing history for the real bundled Stop hook.
  await fs.unlink(path.join(f.config.paths.stateDir, cursors[0]));
  await f.invoke("UserPromptSubmit", {
    prompt: "first turn",
    transcript_path: transcript,
  });
  await f.invoke("Stop", { transcript_path: transcript });
  assert(
    f.received.length > 0,
    "normal Stop exports without SessionEnd fallback",
  );
  await fs.appendFile(
    transcript,
    row("event_msg", { type: "task_started", turn_id: "t2" }) +
      row("event_msg", {
        type: "task_complete",
        turn_id: "t2",
        last_agent_message: "next-done",
      }),
  );
  await f.invoke("UserPromptSubmit", {
    turn_id: "t2",
    prompt: "second turn",
    transcript_path: transcript,
  });
  await f.invoke("Stop", { turn_id: "t2", transcript_path: transcript });
  const spans = f.received
    .flatMap((r) => r.body.resourceSpans ?? [])
    .flatMap((r) => r.scopeSpans)
    .flatMap((r) => r.spans);
  assert.equal(spans.filter((s) => s.name === "codex").length, 2);
  const fresh = new RolloutReader(transcript, "s", f.config, "t2");
  await fresh.read();
  assert.equal(
    fresh.messages.length,
    0,
    "only this exported turn's cursor advanced",
  );
});

test("oversize JSONL records spanning chunks do not prevent terminal detection or cursor resume", async (t) => {
  const f = await fixture(t),
    transcript = path.join(f.env.CODEX_HOME, "sessions/oversize.jsonl");
  await fs.writeFile(
    transcript,
    row("session_meta", { id: "s" }) +
      "x".repeat(5 * 1024 * 1024) +
      "\n" +
      row("event_msg", { type: "task_complete", turn_id: "t" }),
  );
  const first = new RolloutReader(transcript, "s", f.config);
  await first.read();
  await first.checkpoint();
  const resumed = new RolloutReader(transcript, "s", f.config);
  await resumed.read();
  assert(resumed.completed.has("t"));
  await resumed.checkpoint();
});

test("native send_input binds each source turn explicitly, including across a retained done journal", async (t) => {
  const f = await fixture(t),
    store = new EventStore(f.config);
  const event = (name, session, turn, rest = {}) => ({
    hook_event_name: name,
    session_id: session,
    turn_id: turn,
    cwd: f.repo,
    receivedAt: Date.now(),
    ...rest,
  });
  const corr = (e) => correlate(e, f.config);
  await corr(event("UserPromptSubmit", "parent", "p1"));
  await corr(event("SubagentStart", "parent", "c1", { agent_id: "child" }));
  const first = await corr(event("PreModelRequest", "child", "c1"));
  assert.equal(first.turn_id, "p1");
  await store.append(first);
  await store.take(first);
  await corr(event("Stop", "parent", "p1"));
  await corr(event("UserPromptSubmit", "parent", "p2"));
  await corr(
    event("PreToolUse", "parent", "p2", {
      tool_use_id: "send",
      dispatch_target: "child",
    }),
  );
  const prompt = await corr(
    event("UserPromptSubmit", "child", "c2", { prompt: "new work" }),
  );
  assert.equal(prompt.turn_id, "p2");
  assert.equal(prompt.source_turn_id, "c2");
  await corr(
    event("PostToolUse", "parent", "p2", {
      tool_use_id: "send",
      dispatch_turn: "c2",
    }),
  );
  assert(
    !(
      await fs.readdir(path.join(f.config.paths.configDir, "codex-ancestry"))
    ).some((n) => n.endsWith(".dispatch")),
    "completed dispatch state is removed",
  );
  const second = await corr(event("PreModelRequest", "child", "c2"));
  assert.equal(second.turn_id, "p2");
  await store.append(second);
  assert.equal(
    (await store.take(second)).events.length,
    1,
    "new work not discarded by old done marker",
  );
  const late = await corr(event("PostModelRequest", "child", "c1"));
  assert.equal(
    late.turn_id,
    "p1",
    "late prior work is not attributed to newer dispatch",
  );
  const unknown = await corr(
    event("PreModelRequest", "child", "never-dispatched"),
  );
  assert.notEqual(unknown.turn_id, "p1");
  assert(unknown.gap);
  for (let i = 0; i < 10; i++) {
    const source = `concurrent-${i}`,
      call = `dispatch-${i}`;
    await corr(
      event("PreToolUse", "parent", "p2", {
        tool_use_id: call,
        dispatch_target: "child",
      }),
    );
    const [prompt] = await Promise.all([
      corr(event("UserPromptSubmit", "child", source)),
      corr(
        event("PostToolUse", "parent", "p2", {
          tool_use_id: call,
          dispatch_turn: source,
        }),
      ),
    ]);
    assert.equal(prompt.turn_id, "p2");
    assert.equal(prompt.source_turn_id, source);
  }
  for (const tool_name of ["send_input", "multi_agent_v1send_input"]) {
    const sanitized = sanitizeEvent(
      {
        ...event("PreToolUse", "parent", "p2"),
        tool_name,
        tool_input: { target: "child", message: "PRIVATE" },
      },
      { ...f.config, captureContent: false },
    );
    assert.equal(sanitized.dispatch_target, "child");
    assert.equal(sanitized.tool_input, undefined);
  }
});

const flatten = (f) =>
  f.received
    .flatMap((x) => x.body.resourceSpans ?? [])
    .flatMap((x) => x.scopeSpans)
    .flatMap((x) => x.spans);
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((x) => [x.key, Object.values(x.value)[0]]),
  );
test("bundled hook redacts JSON arguments from every reconstructed model output", async (t) => {
  const f = await fixture(t),
    transcript = path.join(f.env.CODEX_HOME, "sessions/privacy.jsonl");
  const secrets = {
    password: "fixture-password",
    token: "fixture-token",
    api_key: "fixture-api-key",
    authorization: "Basic fixture-auth",
  };
  await fs.writeFile(
    transcript,
    row("session_meta", { id: "s" }) +
      row("event_msg", { type: "task_started", turn_id: "t" }),
    { mode: 0o600 },
  );
  await f.invoke("UserPromptSubmit", {
    prompt: "privacy",
    transcript_path: transcript,
  });
  await f.invoke("PreModelRequest", {
    model: "mock",
    transcript_path: transcript,
  });
  await fs.appendFile(
    transcript,
    row("response_item", {
      type: "function_call",
      name: "service",
      arguments: JSON.stringify({ nested: [secrets], safe: "visible-call" }),
    }),
  );
  await f.invoke("PostModelRequest", {
    status: "completed",
    token_usage: { input_tokens: 5, output_tokens: 3 },
    transcript_path: transcript,
  });
  await fs.appendFile(
    transcript,
    row("event_msg", {
      type: "task_complete",
      turn_id: "t",
      last_agent_message: "done",
    }),
  );
  await f.invoke("Stop", { transcript_path: transcript });
  const spans = flatten(f),
    encoded = JSON.stringify(spans);
  for (const secret of Object.values(secrets))
    assert(!encoded.includes(secret));
  assert(
    attrs(spans.find((x) => x.name === "codex.model_attempt"))[
      "output.value"
    ].includes("visible-call"),
  );
});
test("overlapping asynchronous Stops retain both immediate generation outputs in either order", async (t) => {
  for (const reverse of [false, true]) {
    const f = await fixture(t),
      transcript = path.join(f.env.CODEX_HOME, "sessions/overlap.jsonl");
    await fs.writeFile(transcript, row("session_meta", { id: "s" }), {
      mode: 0o600,
    });
    for (const turn_id of ["t", "t2"]) {
      await f.invoke("UserPromptSubmit", {
        turn_id,
        prompt: "prompt-" + turn_id,
        transcript_path: transcript,
      });
      await f.invoke("PreModelRequest", {
        turn_id,
        model: "mock",
        transcript_path: transcript,
      });
      await fs.appendFile(
        transcript,
        row("event_msg", { type: "task_started", turn_id }) +
          row("response_item", {
            type: "message",
            role: "assistant",
            content: [{ text: "output-" + turn_id }],
          }),
      );
      await f.invoke("PostModelRequest", {
        turn_id,
        status: "completed",
        token_usage: { input_tokens: 5, output_tokens: 3 },
        transcript_path: transcript,
      });
      await fs.appendFile(
        transcript,
        row("event_msg", {
          type: "task_complete",
          turn_id,
          last_agent_message: "done-" + turn_id,
        }),
      );
    }
    for (const turn_id of reverse ? ["t2", "t"] : ["t", "t2"])
      await f.invoke("Stop", { turn_id, transcript_path: transcript });
    const spans = flatten(f),
      roots = spans.filter((s) => s.name === "codex");
    assert.equal(roots.length, 2);
    for (const turn_id of ["t", "t2"]) {
      const root = roots.find(
        (s) => attrs(s)["input.value"] === "prompt-" + turn_id,
      );
      const gen = spans.find(
        (s) => s.traceId === root.traceId && s.name === "codex.model_attempt",
      );
      assert.equal(attrs(gen)["output.value"], "output-" + turn_id);
      assert.equal(Number(attrs(gen)["gen_ai.usage.input_tokens"]), 5);
    }
  }
});
test("late terminal row settles beyond three seconds without losing the generation", async (t) => {
  const f = await fixture(t),
    transcript = path.join(f.env.CODEX_HOME, "sessions/late.jsonl");
  await fs.writeFile(
    transcript,
    row("session_meta", { id: "s" }) +
      row("event_msg", { type: "task_started", turn_id: "t" }),
    { mode: 0o600 },
  );
  await f.invoke("UserPromptSubmit", {
    prompt: "late fixture",
    transcript_path: transcript,
  });
  await f.invoke("PreModelRequest", {
    model: "mock",
    transcript_path: transcript,
  });
  await fs.appendFile(
    transcript,
    row("response_item", {
      type: "message",
      role: "assistant",
      content: [{ text: "late output" }],
    }),
  );
  await f.invoke("PostModelRequest", {
    status: "completed",
    transcript_path: transcript,
  });
  const late = new Promise((resolve, reject) =>
    setTimeout(
      () =>
        fs
          .appendFile(
            transcript,
            row("event_msg", {
              type: "task_complete",
              turn_id: "t",
              last_agent_message: "late output",
            }),
          )
          .then(resolve, reject),
      3200,
    ),
  );
  await f.invoke("Stop", { transcript_path: transcript });
  await late;
  assert.equal(flatten(f).filter((s) => s.name === "codex").length, 1);
  assert.equal(
    attrs(flatten(f).find((s) => s.name === "codex.model_attempt"))[
      "output.value"
    ],
    "late output",
  );
});
test("shared external-volume project is ignored, reported, and does not suppress tracing", async (t) => {
  const f = await fixture(t);
  await fs.chmod(f.repo, 0o775);
  await put(path.join(f.repo, ".codex/futureagi.json"), {
    schemaVersion: 1,
    transport: { endpoint: "http://127.0.0.1:1/tracer/v1/traces" },
  });
  const config = await core.resolveConfig(f.options);
  assert.equal(config.enabled, true);
  assert.equal(config.endpoint, f.endpoint);
  assert(config.issues.includes("PROJECT_CONFIG_IGNORED"));
  await f.invoke("UserPromptSubmit", { prompt: "external drive" });
  await f.invoke("Interrupt");
  assert.equal(flatten(f).filter((s) => s.name === "codex").length, 1);
  const status = JSON.parse(
    (await f.run(cli, ["status", "codex", "--json"])).out,
  );
  assert(status.issues.includes("PROJECT_CONFIG_IGNORED"));
});
test("journal append and terminal claim wait for locks and recover a stale owner", async (t) => {
  const f = await fixture(t),
    store = new EventStore(f.config);
  const event = {
    hook_event_name: "UserPromptSubmit",
    session_id: "lock",
    turn_id: "turn",
    cwd: f.repo,
    receivedAt: Date.now(),
  };
  await store.append(event);
  const file = (await fs.readdir(store.dir)).find((n) => n.endsWith(".json"));
  const lock = path.join(store.dir, file + ".journal.write-lock");
  await fs.mkdir(lock, { mode: 0o700 });
  const released = new Promise((resolve, reject) =>
    setTimeout(() => fs.rmdir(lock).then(resolve, reject), 250),
  );
  await store.append({ ...event, hook_event_name: "PreModelRequest" });
  await released;
  await fs.mkdir(lock, { mode: 0o700 });
  const old = new Date(Date.now() - 30000);
  await fs.utimes(lock, old, old);
  const state = await store.take(event);
  assert.equal(state.events.length, 2);
  assert.equal(await store.take(event), undefined);
});
test("compatible host updates and stable interpreter symlinks do not require a binary copy", async (t) => {
  const f = await fixture(t),
    { supportedHostVersion, interpreterPath } = require("../dist/index.js");
  assert(supportedHostVersion("0.167.0"));
  assert(supportedHostVersion("0.168.1"));
  assert(supportedHostVersion("0.167.2"));
  for (const version of ["0.166.9", "1.0.0", "broken", "2026.11.05"])
    assert(!supportedHostVersion(version));
  const bin = path.join(f.root, "bin");
  await fs.mkdir(bin);
  await fs.symlink(process.execPath, path.join(bin, "node"));
  assert.equal(
    await interpreterPath(process.execPath, bin),
    path.join(bin, "node"),
  );
});
test("interactive setup replaces rejected credentials without echo or premature profile writes", async (t) => {
  const f = await fixture(t);
  const args = [cli, "setup", "codex", "--project", "test", "--configure-only"];
  const interactive = (apiKey, secretKey) =>
    new Promise((resolve, reject) => {
      const command = [process.execPath, ...args]
        .map((s) => "'" + s.replaceAll("'", "'\\''") + "'")
        .join(" ");
      const child = spawn("script", ["-q", "-e", "-c", command, "/dev/null"], {
        cwd: f.repo,
        env: { ...f.env, TERM: "xterm" },
      });
      let out = "",
        sentApi = false,
        sentSecret = false;
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        reject(Error("TTY_TIMEOUT"));
      }, 15000);
      child.on("error", reject);
      child.stdout.on("data", (b) => {
        out += b;
        if (!sentApi && out.includes("API key (hidden):")) {
          sentApi = true;
          setTimeout(() => child.stdin.write(apiKey + "\n"), 50);
        }
        if (!sentSecret && out.includes("Secret key (hidden):")) {
          sentSecret = true;
          setTimeout(() => child.stdin.write(secretKey + "\n"), 50);
        }
      });
      child.stderr.on("data", (b) => (out += b));
      child.on("close", (code) => {
        clearTimeout(timer);
        resolve({ out, code });
      });
    });
  f.auth.rejected.add(f.profile.apiKey);
  f.auth.rejected.add("rejected-new-api");
  const before = await fs.readFile(f.config.paths.credentials, "utf8");
  const failed = await interactive("rejected-new-api", "rejected-new-secret");
  assert.equal(failed.code, 2);
  assert(failed.out.includes("AUTH_REJECTED"));
  assert.equal(await fs.readFile(f.config.paths.credentials, "utf8"), before);
  const success = await interactive("replacement-api", "replacement-secret");
  assert.equal(success.code, 0, success.out);
  assert(success.out.includes("CONFIGURED_NOT_REGISTERED"));
  for (const key of [
    "rejected-new-api",
    "rejected-new-secret",
    "replacement-api",
    "replacement-secret",
  ])
    assert(!(failed.out + success.out).includes(key));
  const configured = await core.resolveConfig(f.options);
  assert.equal(configured.credentials.apiKey, "replacement-api");
  assert.equal(configured.credentials.secretKey, "replacement-secret");
});
test("unknown hook shapes leave a content-free gap and do not disable subsequent turns", async (t) => {
  const f = await fixture(t);
  await f.invoke("FutureHookShape", {
    prompt: "must-not-persist-unknown-content",
  });
  await f.invoke("PreModelRequest", { turn_id: undefined, model: "mock" });
  const gap = JSON.parse(
    await fs.readFile(
      path.join(f.config.paths.stateDir, "codex-gap.json"),
      "utf8",
    ),
  );
  assert.equal(gap.code, "UNKNOWN_HOOK_SHAPE");
  assert(!JSON.stringify(gap).includes("must-not-persist-unknown-content"));
  await f.invoke("UserPromptSubmit", { prompt: "known shape" });
  await f.invoke("Interrupt");
  assert.equal(flatten(f).filter((s) => s.name === "codex").length, 1);
});
test("nonterminal capture does not initialize the tracing provider or OTLP exporter", async (t) => {
  const f = await fixture(t),
    blocker = path.join(f.root, "block-exporter.cjs");
  await fs.writeFile(
    blocker,
    `const Module=require('node:module'); const load=Module._load;
Module._load=function(name,...args){if(name==='@traceai/agent-core'||name==='@traceai/fi-core'||name.startsWith('@opentelemetry/'))throw Error('EXPORTER_LOADED');return load.call(this,name,...args);};
require(${JSON.stringify(path.resolve(__dirname, "../dist/hook.js"))});`,
  );
  const result = await f.run(
    blocker,
    [],
    JSON.stringify({
      hook_event_name: "UserPromptSubmit",
      session_id: "light",
      turn_id: "turn",
      cwd: f.repo,
      prompt: "lightweight prompt",
    }),
  );
  assert.deepEqual(result, { code: 0, out: "", err: "" });
  const saved = await new EventStore(f.config).take({
    session_id: "light",
    turn_id: "turn",
  });
  assert.equal(saved.events[0].prompt, "lightweight prompt");
  assert.equal(f.received.length, 0);
});
test("SessionEnd cannot steal an async Stop before late generation content is written", async (t) => {
  const f = await fixture(t),
    transcript = path.join(f.env.CODEX_HOME, "sessions/late-shutdown.jsonl");
  await fs.writeFile(
    transcript,
    row("session_meta", { id: "s" }) +
      row("event_msg", { type: "task_started", turn_id: "t" }),
    { mode: 0o600 },
  );
  await f.invoke("UserPromptSubmit", {
    prompt: "late shutdown",
    transcript_path: transcript,
  });
  await f.invoke("PreModelRequest", {
    model: "mock",
    transcript_path: transcript,
  });
  const delayedOutput = row("response_item", {
    type: "message",
    role: "assistant",
    content: [{ text: "settled generation" }],
  });
  await f.invoke("PostModelRequest", {
    status: "completed",
    token_usage: { input_tokens: 5, output_tokens: 2 },
    transcript_path: transcript,
  });
  const late = new Promise((resolve, reject) =>
    setTimeout(
      () =>
        fs
          .appendFile(
            transcript,
            delayedOutput +
              row("event_msg", {
                type: "task_complete",
                turn_id: "t",
                last_agent_message: "settled generation",
              }),
          )
          .then(resolve, reject),
      3200,
    ),
  );
  const stop = f.invoke("Stop", { transcript_path: transcript });
  await new Promise((resolve) => setTimeout(resolve, 500));
  await f.invoke("SessionEnd", {
    turn_id: "shutdown",
    transcript_path: transcript,
  });
  const earlyRoots = flatten(f).filter((s) => s.name === "codex").length;
  await Promise.all([stop, late]);
  assert.equal(
    earlyRoots,
    0,
    "leave pending content with the asynchronous observer",
  );
  const spans = flatten(f);
  assert.equal(spans.filter((s) => s.name === "codex").length, 1);
  assert.equal(
    attrs(spans.find((s) => s.name === "codex.model_attempt"))["output.value"],
    "settled generation",
  );
});

test("reused grandchild retains root coordinates and immediate parent across dispatch races", async (t) => {
  const f = await fixture(t);
  const send = (hook_event_name, session_id, turn_id, extra = {}) =>
    correlate(
      {
        hook_event_name,
        session_id,
        turn_id,
        cwd: f.repo,
        receivedAt: Date.now(),
        ...extra,
      },
      f.config,
    );
  await send("UserPromptSubmit", "root", "root-turn");
  await send("SubagentStart", "root", "child-turn", { agent_id: "child" });
  await send("UserPromptSubmit", "child", "child-turn");
  for (const rootEnvelope of [false, true]) {
    const session = rootEnvelope ? "root" : "child";
    const grand = rootEnvelope ? "grand-envelope" : "grand";
    const envelope = rootEnvelope ? { agent_id: "child" } : {};
    await send("SubagentStart", session, "grand-turn", { agent_id: grand });
    for (const promptFirst of [true, false]) {
      const turn = grand + (promptFirst ? "-reuse-1" : "-reuse-2");
      await send("PreToolUse", session, "child-turn", {
        ...envelope,
        tool_use_id: turn,
        dispatch_target: grand,
      });
      if (promptFirst) await send("UserPromptSubmit", grand, turn);
      await send("PostToolUse", session, "child-turn", {
        ...envelope,
        tool_use_id: turn,
        dispatch_turn: turn,
      });
      if (!promptFirst) await send("UserPromptSubmit", grand, turn);
      const e = await send("PreModelRequest", grand, turn);
      assert.equal(e.session_id, "root");
      assert.equal(e.turn_id, "root-turn");
      assert.equal(e.parent_agent_id, "child");
      assert.equal(e.parent_turn_id, "child-turn");
      assert.equal(e.source_turn_id, turn);
    }
  }
});

test("bundled hook redacts quoted assignments, escapes, and all secret aliases on every span", async (t) => {
  const f = await fixture(t);
  const values = [
    "quoted-password",
    "quoted-token",
    "quoted-api",
    "alias-secret",
    "alias-aws",
    "alias-dashed",
    "escaped-double",
    "escaped-single",
    "structured-secret",
    "structured-aws",
  ];
  const text = `PASSWORD="${values[0]} with spaces" TOKEN='${values[1]}' API_KEY="${values[2]}" secret_key=${values[3]} AWS_SECRET_ACCESS_KEY=${values[4]} secret-access-key='${values[5]}' password="part\\"${values[6]}" token='part\\'${values[7]}' visible-marker`;
  await f.invoke("UserPromptSubmit", { prompt: text });
  await f.invoke("PreToolUse", {
    tool_name: "service",
    tool_use_id: "quoted",
    tool_input: {
      command: text,
      secret_access_key: values[8],
      AWS_SECRET_ACCESS_KEY: values[9],
      safe: "visible-marker",
    },
  });
  await f.invoke("PostToolUse", {
    tool_name: "service",
    tool_use_id: "quoted",
    tool_response: text,
  });
  await f.invoke("Interrupt", { last_assistant_message: text });
  const spans = flatten(f);
  assert.equal(spans.length, 2);
  for (const span of spans) {
    const encoded = JSON.stringify(span);
    for (const value of values)
      assert(!encoded.includes(value), `leaked synthetic marker ${value}`);
    assert(encoded.includes("visible-marker"));
    assert(/\[REDACTED\]|<redacted>/.test(encoded));
  }
});

for (const long of [false, true])
  test(`successful bundled Stop preserves current content after ${long ? "84 MiB" : "513 messages"} of history`, async (t) => {
    const f = await fixture(t),
      transcript = path.join(f.env.CODEX_HOME, "sessions/history.jsonl");
    await fs.writeFile(
      transcript,
      row("session_meta", { id: "s" }) +
        row("event_msg", { type: "task_started", turn_id: "old" }),
      { mode: 0o600 },
    );
    const old = row("response_item", {
      type: "message",
      role: "assistant",
      content: [{ text: long ? "h".repeat(120000) : "old-message" }],
    });
    await fs.appendFile(transcript, old.repeat(long ? 734 : 513));
    if (long) assert((await fs.stat(transcript)).size > 80 * 1024 * 1024);
    await fs.appendFile(
      transcript,
      row("event_msg", { type: "task_started", turn_id: "t" }),
    );
    await f.invoke("UserPromptSubmit", {
      prompt: "new prompt",
      transcript_path: transcript,
    });
    await f.invoke("PreModelRequest", {
      model: "mock",
      transcript_path: transcript,
    });
    await fs.appendFile(
      transcript,
      row("response_item", {
        type: "message",
        role: "assistant",
        content: [{ text: "current output" }],
      }),
    );
    await f.invoke("PostModelRequest", {
      status: "completed",
      token_usage: { input_tokens: 5, output_tokens: 3 },
      transcript_path: transcript,
    });
    await fs.appendFile(
      transcript,
      row("event_msg", {
        type: "task_complete",
        turn_id: "t",
        last_agent_message: "current output",
      }),
    );
    await f.invoke("Stop", { transcript_path: transcript });
    const spans = flatten(f),
      root = spans.find((s) => s.name === "codex"),
      gen = spans.find((s) => s.name === "codex.model_attempt");
    assert.equal(spans.length, 2);
    assert.equal(attrs(root)["fi.agent.outcome"], "success");
    assert.equal(attrs(gen)["fi.agent.outcome"], "success");
    assert.equal(attrs(gen)["input.value"], "new prompt");
    assert.equal(attrs(gen)["output.value"], "current output");
    assert.equal(Number(attrs(gen)["gen_ai.usage.input_tokens"]), 5);
    assert.equal(Number(attrs(gen)["gen_ai.usage.output_tokens"]), 3);
  });

test("engine reports incomplete enrichment when its independent read budget is exhausted", async (t) => {
  const f = await fixture(t),
    { CodexEngine } = require("../dist/engine.js");
  const reader = {
    matches: () => true,
    read: async () => {},
    checkpoint: async () => {},
    hasMore: true,
    targetSeen: false,
    gap: false,
    messages: [],
    toolResults: new Map(),
  };
  const tracer = {
    startTurn: () => ({
      end: (v) => {
        assert.equal(v.outcome, "incomplete");
      },
      child: () => {
        throw Error("unexpected child");
      },
    }),
    flush: async () => {},
    shutdown: async () => {},
  };
  const engine = new CodexEngine(f.config, tracer, [reader]);
  const event = {
    session_id: "s",
    turn_id: "t",
    receivedAt: Date.now(),
    cwd: f.repo,
    transcript_path: "/unread.jsonl",
  };
  await engine.accept({
    ...event,
    hook_event_name: "UserPromptSubmit",
    prompt: "budget",
  });
  await engine.accept({ ...event, hook_event_name: "Stop" });
});

for (const change of ["setup", "repository"]) {
  test(`pending asynchronous Stop discards content after ${change} opt-out`, async (t) => {
    const f = await fixture(t);
    const initial = await f.run(cli, [
      "setup",
      "codex",
      "--project",
      "test",
      "--configure-only",
      "--json",
    ]);
    assert.equal(initial.code, 0, initial.out);
    const transcript = path.join(f.env.CODEX_HOME, "sessions/policy.jsonl");
    await fs.writeFile(
      transcript,
      row("session_meta", { id: "s" }) +
        row("event_msg", { type: "task_started", turn_id: "t" }),
      { mode: 0o600 },
    );
    await f.invoke("UserPromptSubmit", {
      prompt: "REVOKED_PRIVATE_PROMPT",
      transcript_path: transcript,
    });
    await f.invoke("PreModelRequest", {
      model: "mock",
      transcript_path: transcript,
    });
    await f.invoke("PostModelRequest", {
      status: "completed",
      transcript_path: transcript,
    });
    const pending = f.invoke("Stop", { transcript_path: transcript });
    await new Promise((resolve) => setTimeout(resolve, 700));
    if (change === "setup") {
      const optout = await f.run(cli, [
        "setup",
        "codex",
        "--project",
        "test",
        "--configure-only",
        "--no-capture-content",
        "--json",
      ]);
      assert.equal(optout.code, 0, optout.out);
    } else {
      await put(path.join(f.repo, ".codex/futureagi.json"), {
        schemaVersion: 1,
        captureContent: false,
      });
    }
    assert.equal((await core.resolveConfig(f.options)).captureContent, false);
    const before = f.received.length;
    await fs.appendFile(
      transcript,
      row("response_item", {
        type: "message",
        role: "assistant",
        content: [{ text: "REVOKED_PRIVATE_OUTPUT" }],
      }) +
        row("event_msg", {
          type: "task_complete",
          turn_id: "t",
          last_agent_message: "REVOKED_PRIVATE_OUTPUT",
        }),
    );
    await pending;
    assert.equal(
      f.received.length,
      before,
      "a revoked snapshot must not send any batch",
    );
    assert.equal(flatten(f).length, 0);
    await f.invoke("UserPromptSubmit", {
      turn_id: "next",
      prompt: "NEW_PRIVATE_PROMPT",
    });
    await f.invoke("Interrupt", { turn_id: "next" });
    assert.equal(flatten(f).filter((s) => s.name === "codex").length, 1);
    assert(
      !JSON.stringify(flatten(f)).includes("PRIVATE_"),
      "later metadata-only turns still work",
    );
  });
}

test("nested serialized secrets and escaped assignments never escape direct or reconstructed tool content", async (t) => {
  const f = await fixture(t);
  const markers = [
    "ESCAPED_PRIVATE_VALUE",
    "NESTED_PRIVATE_VALUE",
    "BASIC_PRIVATE_VALUE",
  ];
  const output =
    "Process exited with code 0\nFinal output:\n" +
    JSON.stringify({
      payload: 'PASSWORD="' + markers[0] + ' with \\"embedded\\" quotes"',
      nested: JSON.stringify({ credentials: { password: markers[1] } }),
      header: "Authorization: Basic " + markers[2],
      safe: "visible-safe-result",
    });
  await f.invoke("UserPromptSubmit", { prompt: "privacy" });
  await f.invoke("PreToolUse", {
    tool_name: "exec_command",
    tool_use_id: "encoded",
    tool_input: output,
  });
  await f.invoke("PostToolUse", {
    tool_name: "exec_command",
    tool_use_id: "encoded",
    tool_response: output,
  });
  await f.invoke("Interrupt");
  const spans = flatten(f);
  assert.equal(
    spans.filter((s) => attrs(s)["fi.span.kind"] === "TOOL").length,
    1,
  );
  const body = JSON.stringify(spans);
  for (const marker of markers) assert(!body.includes(marker), marker);
  assert(body.includes("visible-safe-result"));
});

for (const recover of [false, true]) {
  test(`unified core reports terminal failure and successful later retry (recover=${recover})`, async (t) => {
    const f = await fixture(t);
    const { CodexEngine } = require("../dist/engine.js");
    const engine = new CodexEngine(f.config);
    let at = Date.now();
    const accept = (hook_event_name, rest = {}) =>
      engine.accept({
        hook_event_name,
        session_id: "s",
        turn_id: "failure",
        cwd: f.repo,
        receivedAt: at++,
        ...rest,
      });
    await accept("UserPromptSubmit", { prompt: "model failure" });
    await accept("PreModelRequest", { model: "mock" });
    await accept("PostModelRequest", {
      status: "failed",
      error: "synthetic provider failure",
    });
    if (recover) {
      await accept("PreModelRequest", { model: "mock" });
      await accept("PostModelRequest", { status: "completed" });
    }
    await accept("Stop");
    await engine.close();
    const root = flatten(f).find((s) => s.name === "codex");
    assert.equal(
      attrs(root)["fi.agent.outcome"],
      recover ? "success" : "error",
    );
    assert.equal(root.status.code, recover ? 1 : 2);
  });
}
