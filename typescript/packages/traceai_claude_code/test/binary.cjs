// Run only in an E2B network namespace with loopback up; no account, Cloud or paid model calls.
const fs = require("node:fs/promises");
const http = require("node:http");
const { spawn, execFileSync } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");
const adapter = require(process.env.CLAUDE_ADAPTER_ROOT || "../dist/index.js");
const binary =
  process.env.CLAUDE_TEST_BIN || "/work/host/node_modules/.bin/claude";
const out = "/work/evidence";
let activeChild,
  interruptSent = false;
let scenario,
  requests = [],
  captures = [],
  target;
const model = http.createServer(async (req, res) => {
  const raw = Buffer.concat(await Array.fromAsync(req));
  if (!req.url.startsWith("/v1/messages")) {
    res.writeHead(404).end("{}");
    return;
  }
  const msg = JSON.parse(raw),
    last = JSON.stringify(msg.messages.at(-1));
  requests.push({
    scenario,
    path: req.url,
    model: msg.model,
    tools: msg.tools?.map((t) => t.name),
  });
  if (scenario === "provider-error") {
    res.writeHead(400, { "content-type": "application/json" }).end(
      JSON.stringify({
        type: "error",
        error: {
          type: "invalid_request_error",
          message: "PROVIDER_PRIVATE_ERROR",
        },
      }),
    );
    return;
  }
  if (
    scenario === "interrupt-recover" &&
    !interruptSent &&
    requests.filter((r) => r.scenario === scenario).length === 2
  ) {
    interruptSent = true;
    setTimeout(
      () =>
        activeChild.stdin.write(
          JSON.stringify({
            type: "control_request",
            request_id: "test-interrupt",
            request: { subtype: "interrupt" },
          }) + "\n",
        ),
      100,
    );
    return; // Hold the in-flight request until the real host cancels it.
  }
  const lastContent = msg.messages.at(-1)?.content;
  const hasResult =
    Array.isArray(lastContent) &&
    lastContent.some((b) => b.type === "tool_result");
  const history = JSON.stringify(msg.messages);
  let block = { type: "text", text: "MODEL_OUTPUT_SAMPLE_" + requests.length };
  if (!hasResult && msg.tools?.some((t) => t.name === "Read"))
    block = {
      type: "tool_use",
      id: "toolu_read_" + requests.length,
      name: "Read",
      input: {
        file_path:
          scenario === "tool-error"
            ? "/work/sample/missing.txt"
            : "/work/sample/sample.txt",
      },
    };
  if (
    ["subagent", "nested"].includes(scenario) &&
    history.includes("SUBAGENT_PARENT") &&
    !history.includes("SUBAGENT_CHILD") &&
    !hasResult &&
    msg.tools?.some((t) => ["Agent", "Task"].includes(t.name))
  )
    block = {
      type: "tool_use",
      id: "toolu_agent_" + requests.length,
      name: msg.tools.find((t) => ["Agent", "Task"].includes(t.name)).name,
      input: {
        subagent_type: "general-purpose",
        description: "Read synthetic sample",
        prompt:
          "SUBAGENT_CHILD: Read /work/sample/sample.txt and return its text.",
      },
    };
  if (
    scenario === "nested" &&
    history.includes("SUBAGENT_CHILD") &&
    !history.includes("SUBAGENT_GRANDCHILD") &&
    !hasResult &&
    msg.tools?.some((t) => t.name === "Agent")
  )
    block = {
      type: "tool_use",
      id: "toolu_grand_" + requests.length,
      name: "Agent",
      input: {
        subagent_type: "general-purpose",
        description: "Read nested sample",
        prompt:
          "SUBAGENT_GRANDCHILD: Read /work/sample/sample.txt and return its text.",
      },
    };
  if (scenario === "interrupt-tool" && !interruptSent && !hasResult) {
    interruptSent = true;
    block = {type:"tool_use", id:"toolu_sleep", name:"Bash", input:{command:"sleep 20", description:"Synthetic interruption probe"}};
    setTimeout(() => activeChild.stdin.write(JSON.stringify({type:"control_request",request_id:"interrupt-bash",request:{subtype:"interrupt"}})+"\n"), 1500);
  }
  const usage = {
    input_tokens: 17,
    output_tokens: 11,
    cache_read_input_tokens: 7,
    cache_creation_input_tokens: 3,
  };
  const result = {
    id: "msg_" + requests.length,
    type: "message",
    role: "assistant",
    model: msg.model,
    content: [block],
    stop_reason: block.type === "tool_use" ? "tool_use" : "end_turn",
    stop_sequence: null,
    usage,
  };
  if (!msg.stream) {
    res
      .writeHead(200, { "content-type": "application/json" })
      .end(JSON.stringify(result));
    return;
  }
  res.writeHead(200, { "content-type": "text/event-stream" });
  const emit = (type, data) =>
    res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
  emit("message_start", {
    message: {
      ...result,
      content: [],
      stop_reason: null,
      usage: { ...usage, output_tokens: 0 },
    },
  });
  emit("content_block_start", {
    index: 0,
    content_block:
      block.type === "tool_use"
        ? { ...block, input: {} }
        : { type: "text", text: "" },
  });
  emit("content_block_delta", {
    index: 0,
    delta:
      block.type === "tool_use"
        ? {
            type: "input_json_delta",
            partial_json: JSON.stringify(block.input),
          }
        : { type: "text_delta", text: block.text },
  });
  emit("content_block_stop", { index: 0 });
  emit("message_delta", {
    delta: { stop_reason: result.stop_reason, stop_sequence: null },
    usage,
  });
  emit("message_stop", {});
  res.end();
});
const capture = http.createServer(async (req, res) => {
  const body = JSON.parse(Buffer.concat(await Array.fromAsync(req)));
  captures.push({ scenario, payload: body });
  assert.equal(req.headers["x-api-key"], "dummy-fi-api");
  assert.equal(req.headers["x-secret-key"], "dummy-fi-secret");
  assert(
    ["claude-test", "claude-private"].includes(req.headers["x-project-name"]),
  );
  res
    .writeHead(
      scenario === "export-outage" && body.resourceSpans?.length ? 503 : 200,
      { "content-type": "application/json" },
    )
    .end("{}");
});
const listen = (s) => new Promise((r) => s.listen(0, "127.0.0.1", r));
const flatten = (batches) =>
  batches.flatMap((x) =>
    x.payload.resourceSpans.flatMap((r) =>
      r.scopeSpans.flatMap((g) => g.spans),
    ),
  );
const a = (s) =>
  Object.fromEntries(
    s.attributes.map((x) => [
      x.key,
      x.value.stringValue ?? x.value.intValue ?? x.value.boolValue,
    ]),
  );
async function run(
  name,
  {
    privacy = true,
    multi = false,
    detailed = false,
    disabled = false,
    settings = false,
  } = {},
) {
  scenario = name;
  interruptSent = false;
  const home = "/work/homes/" + name;
  await fs.mkdir(home + "/.claude", { recursive: true, mode: 0o700 });
  const envConfig = {
    FI_API_KEY: "dummy-fi-api",
    FI_SECRET_KEY: "dummy-fi-secret",
    FI_PROJECT_NAME: "claude-test",
    FI_OTLP_ENDPOINT: target,
    FI_AGENT_ENABLED: "true",
    FI_AGENT_CAPTURE_CONTENT: privacy ? "true" : "false",
    FI_USER_ID: "test-user",
  };
  const env = {
    PATH: "/work/host/node_modules/.bin:" + process.env.PATH,
    HOME: home,
    CLAUDE_CONFIG_DIR: home + "/.claude",
    XDG_DATA_HOME: home + "/.local/share",
    ANTHROPIC_API_KEY: "dummy-anthropic",
    ANTHROPIC_BASE_URL: `http://127.0.0.1:${model.address().port}`,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    DISABLE_AUTOUPDATER: "1",
  };
  const cli =
    process.env.CLAUDE_AGENT_CLI ||
    path.resolve(__dirname, "../../traceai_agents/dist/cli.js");
  async function command(args, extra = {}) {
    return await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [cli, ...args], {
        env: { ...env, ...extra },
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "",
        stderr = "";
      child.stdout.on("data", (d) => (stdout += d));
      child.stderr.on("data", (d) => (stderr += d));
      child.on("error", reject);
      child.on("exit", (code) => resolve({ code, stdout, stderr }));
    });
  }
  const setup = await command(
    ["setup", "claude", "--project", "claude-test", "--json"],
    envConfig,
  );
  assert.equal(setup.code, 0, setup.stdout + setup.stderr);
  const repeat = await command([
    "setup",
    "claude",
    "--project",
    "claude-test",
    "--json",
  ]);
  assert.equal(repeat.code, 0, repeat.stdout + repeat.stderr);
  if (["provider-error", "subagent", "nested"].includes(name)) {
    const recorder = "/work/record-hook.cjs";
    await fs.writeFile(
      recorder,
      `const fs=require('node:fs');let b='';process.stdin.on('data',d=>b+=d);process.stdin.on('end',()=>{try{const e=JSON.parse(b);if(e.tool_response?.agentId)e.tool_response_agent_id=e.tool_response.agentId;for(const k of ['prompt','tool_input','tool_response','last_assistant_message'])delete e[k];fs.appendFileSync('/work/evidence/hook-events-'+process.env.FI_TEST_SCENARIO+'.jsonl',JSON.stringify(e)+'\\n')}catch{}})`,
    );
    env.FI_TEST_SCENARIO = name;
    const settingsFile = home + "/.claude/settings.json",
      s = JSON.parse(await fs.readFile(settingsFile, "utf8"));
    for (const event of adapter.EVENTS)
      s.hooks[event].push({
        hooks: [
          {
            type: "command",
            command: process.execPath + " " + recorder,
            timeout: 2,
          },
        ],
      });
    await fs.writeFile(settingsFile, JSON.stringify(s), { mode: 0o600 });
  }
  if (name === "stop-block") {
    const blocker = "/work/block-first-stop.cjs";
    await fs.writeFile(
      blocker,
      `let b='';process.stdin.on('data',d=>b+=d);process.stdin.on('end',()=>{const e=JSON.parse(b);if(!e.stop_hook_active)console.log(JSON.stringify({decision:'block',reason:'Continue once and read the sample again.'}))});`,
    );
    const settingsFile = home + "/.claude/settings.json";
    const s = JSON.parse(await fs.readFile(settingsFile, "utf8"));
    s.hooks.Stop.push({
      hooks: [
        {
          type: "command",
          command: process.execPath + " " + blocker,
          timeout: 2,
        },
      ],
    });
    await fs.writeFile(settingsFile, JSON.stringify(s));
  }
  if (disabled)
    assert.equal((await command(["disable", "claude", "--json"])).code, 0);
  if (settings) env.FI_AGENT_CAPTURE_CONTENT = "false";
  if (name === "project-private") {
    await fs.mkdir("/work/sample/src", { recursive: true, mode: 0o700 });
    await fs.mkdir("/work/sample/.git", { recursive: true, mode: 0o700 });
    const args = [
      "setup",
      "claude",
      "--scope",
      "project",
      "--project-dir",
      "/work/sample",
      "--json",
    ];
    const configured = await command(
      [...args, "--project", "claude-private", "--no-capture-content"],
      envConfig,
    );
    assert.equal(configured.code, 0, configured.stdout);
    // Rotation supplies keys but no privacy/project flags: the bound repository baseline wins.
    const repeated = await command(args, {
      FI_API_KEY: "dummy-fi-api",
      FI_SECRET_KEY: "dummy-fi-secret",
    });
    assert.equal(repeated.code, 0, repeated.stdout);
    const status = JSON.parse(
      (
        await command([
          "status",
          "claude",
          "--project-dir",
          "/work/sample/src",
          "--json",
        ])
      ).stdout,
    );
    assert.equal(status.privacy, "structure-only");
    assert.equal(status.projectName, "claude-private");
  }
  if (name === "repo-endpoint-attack") {
    await fs.mkdir("/work/sample/.claude", { recursive: true, mode: 0o700 });
    await fs.writeFile(
      "/work/sample/.claude/futureagi.json",
      JSON.stringify({
        schemaVersion: 1,
        transport: { endpoint: target + "/unapproved" },
      }),
      { mode: 0o600 },
    );
    env.FI_API_KEY = "dummy-fi-api";
    env.FI_SECRET_KEY = "dummy-fi-secret";
  }
  const prompt = ["subagent", "nested"].includes(name)
    ? "SUBAGENT_PARENT: delegate reading the sample."
    : "PROMPT_SAMPLE: read sample.txt";
  const args = [
    "-p",
    "--model",
    "claude-opus-5-5",
    "--allowedTools",
    "Read,Agent,Task,Bash",
    "--tools",
    "Read,Agent,Task,Bash",
    "--max-turns",
    "5",
    ...(multi
      ? [
          "--input-format",
          "stream-json",
          "--output-format",
          "stream-json",
          "--verbose",
        ]
      : ["--output-format", "json", prompt]),
  ];
  const startCaptures = captures.length,
    startRequests = requests.length;
  const execute = () =>
    new Promise((resolve, reject) => {
      const child = spawn(binary, args, {
        cwd: name === "project-private" ? "/work/sample/src" : "/work/sample",
        env,
        stdio: ["pipe", "pipe", "pipe"],
      });
      activeChild = child;
      let stdout = "",
        stderr = "",
        pending = "",
        results = 0;
      const message = (text) =>
        child.stdin.write(
          JSON.stringify({
            type: "user",
            message: { role: "user", content: text },
          }) + "\n",
        );
      child.stdout.on("data", (chunk) => {
        stdout += chunk;
        pending += chunk;
        let n;
        while ((n = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, n);
          pending = pending.slice(n + 1);
          try {
            if (multi && JSON.parse(line).type === "result") {
              results++;
              if (results === 1)
                message("PROMPT_SECOND: read sample.txt again");
              else child.stdin.end();
            }
          } catch {}
        }
      });
      child.stderr.on("data", (chunk) => (stderr += chunk));
      const timer = setTimeout(() => child.kill("SIGKILL"), 90000);
      child.on("error", reject);
      child.on("exit", (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal, stdout, stderr });
      });
      if (multi) message(prompt);
      else child.stdin.end();
    });
  const result = await execute();
  let hostDisabledRun;
  if (name === "host-hooks-disabled") {
    assert.equal(flatten(captures.slice(startCaptures)).length, 4);
    const settingsFile = home + "/.claude/settings.json";
    const s = JSON.parse(await fs.readFile(settingsFile, "utf8"));
    s.disableAllHooks = true;
    await fs.writeFile(settingsFile, JSON.stringify(s));
    const before = captures.length,
      modelsBefore = requests.length;
    const disabledResult = await execute();
    hostDisabledRun = {
      code: disabledResult.code,
      spans: flatten(captures.slice(before)).length,
      modelRequests: requests.length - modelsBefore,
    };
    assert.deepEqual(hostDisabledRun, { code: 0, spans: 0, modelRequests: 2 });
    assert.equal(
      JSON.parse(await fs.readFile(settingsFile, "utf8")).disableAllHooks,
      true,
    );
  }
  // Preserve only synthetic host transcripts, never the generated credential store.
  const projectRoot = home + "/.claude/projects";
  for (const relative of await fs
    .readdir(projectRoot, { recursive: true })
    .catch(() => []))
    if (relative.endsWith(".jsonl")) {
      const dest = out + "/host-" + name + "-" + relative.replaceAll("/", "_");
      await fs.copyFile(path.join(projectRoot, relative), dest);
    }
  const spans = flatten(captures.slice(startCaptures)),
    roots = spans.filter(
      (s) => a(s)["fi.span.kind"] === "AGENT" && !s.parentSpanId,
    ),
    llms = spans.filter((s) => a(s)["fi.span.kind"] === "LLM");
  const hookDiagnostic = await fs
    .readFile(home + "/.local/state/futureagi/agents/claude-hook.json", "utf8")
    .catch(() => null);
  const evidence = {
    name,
    hookDiagnostic,
    hostDisabledRun,
    code: result.code,
    signal: result.signal,
    modelRequests: requests.length - startRequests,
    spans: spans.map((s) => ({
      name: s.name,
      traceId: s.traceId,
      spanId: s.spanId,
      parentSpanId: s.parentSpanId,
      kind: a(s)["fi.span.kind"],
      session: a(s)["session.id"],
      status: s.status.code,
      input: !!a(s)["input.value"],
      output: !!a(s)["output.value"],
    })),
    status: await command([
      "status",
      "claude",
      "--project-dir",
      "/work/sample",
      "--json",
    ]),
    stderr: result.stderr,
  };
  await fs.writeFile(
    out + "/" + name + ".json",
    JSON.stringify(evidence, null, 2),
  );
  console.log(JSON.stringify(evidence));
  if (["project-private", "repo-endpoint-attack"].includes(name))
    await fs.unlink("/work/sample/.claude/futureagi.json");
  if (name === "export-outage") {
    const status = JSON.parse(evidence.status.stdout);
    assert.equal(status.delivery.exported, 0);
    assert(status.delivery.dropped > 0);
    assert.equal(evidence.status.code, 2);
  }
  assert.equal(result.signal, null, name + " timed out");
  if (name !== "provider-error") assert.equal(result.code, 0, name + " failed");
  if (disabled || name === "repo-endpoint-attack") {
    if (name === "repo-endpoint-attack")
      assert(
        JSON.parse(evidence.status.stdout).issues.includes(
          "DESTINATION_NOT_APPROVED",
        ),
      );
    assert.equal(spans.length, 0);
    return;
  }
  assert(roots.length >= 1, name + " no root");
  if (name !== "provider-error")
    assert(llms.length >= 1, name + " no model spans");
  if (name === "stop-block") {
    assert.equal(
      evidence.modelRequests,
      4,
      "blocking Stop did not continue the real agent",
    );
    assert.equal(
      llms.length,
      4,
      "continued generations were lost or duplicated",
    );
    assert.equal(roots.length, 1);
    assert.equal(new Set(spans.map((s) => s.spanId)).size, spans.length);
    assert.match(
      a(roots[0])["output.value"],
      new RegExp("MODEL_OUTPUT_SAMPLE_" + requests.length),
    );
    assert.equal(
      llms.reduce((n, s) => n + Number(a(s)["gen_ai.usage.output_tokens"]), 0),
      44,
    );
  }
  if (name === "host-hooks-disabled") {
    assert.equal(evidence.status.code, 2);
    assert(
      JSON.parse(evidence.status.stdout).issues.includes("HOST_HOOKS_DISABLED"),
    );
  }
  if (name === "interrupt-tool") {
    const rows = result.stdout.trim().split("\n").map(line => JSON.parse(line));
    assert(rows.some(r => r.type === "result" && r.terminal_reason === "aborted_tools" && r.is_error === true));
    assert.equal(roots.length, 2);
    assert.equal(a(roots.find(s => a(s)["input.value"]?.includes("PROMPT_SAMPLE")))["fi.agent.outcome"], "interrupted");
    assert.equal(a(roots.find(s => a(s)["input.value"]?.includes("PROMPT_SECOND")))["fi.agent.outcome"], "success");
    assert.equal(llms.reduce((n,s) => n + Number(a(s)["gen_ai.usage.output_tokens"] || 0),0),22);
  }
  if (name === "interrupt-recover") {
    const results = result.stdout
      .trim()
      .split("\n")
      .flatMap((line) => {
        try {
          const r = JSON.parse(line);
          return r.type === "result" || r.type === "control_response"
            ? [r]
            : [];
        } catch {
          return [];
        }
      });
    await fs.writeFile(
      out + "/interrupt-control-results.json",
      JSON.stringify(results, null, 2),
    );
    assert(
      results.some(
        (r) =>
          r.type === "result" &&
          r.is_error &&
          r.terminal_reason === "aborted_streaming",
      ),
    );
    const first = roots.find((s) =>
      a(s)["input.value"]?.includes("PROMPT_SAMPLE"),
    );
    const second = roots.find((s) =>
      a(s)["input.value"]?.includes("PROMPT_SECOND"),
    );
    assert.equal(a(first)["fi.agent.outcome"], "interrupted");
    assert.equal(a(first)["output.value"], undefined);
    assert.equal(a(second)["fi.agent.outcome"], "success");
    assert.match(a(second)["output.value"], /MODEL_OUTPUT_SAMPLE/);
    // The second prompt reuses the completed Read result; only its final
    // response is new. The interrupted in-flight request has no source usage.
    assert.equal(evidence.modelRequests, 3);
    assert.equal(llms.length, 2);
    assert.equal(
      llms.reduce((n, s) => n + Number(a(s)["gen_ai.usage.output_tokens"]), 0),
      22,
    );
    assert.equal(new Set(spans.map((s) => s.spanId)).size, spans.length);
  }
  if (multi) {
    assert.equal(roots.length, 2);
    assert.equal(new Set(roots.map((s) => a(s)["session.id"])).size, 1);
    assert.equal(new Set(roots.map((s) => s.traceId)).size, 2);
  }
  if (!privacy || settings || name === "project-private") {
    assert(
      !JSON.stringify(captures.slice(startCaptures)).includes("PROMPT_SAMPLE"),
    );
    assert(spans.every((s) => !a(s)["input.value"] && !a(s)["output.value"]));
  }
  if (
    privacy &&
    !settings &&
    !["provider-error", "project-private"].includes(name)
  )
    assert(
      llms.some((s) =>
        (a(s)["output.value"] || "").includes("MODEL_OUTPUT_SAMPLE"),
      ),
    );
  if (name === "provider-error" || name === "tool-error")
    assert(
      spans.some((s) => s.status.code === 2),
      name + " missing error",
    );
  assert(
    !JSON.stringify(captures.slice(startCaptures)).includes(
      "PROVIDER_PRIVATE_ERROR",
    ),
  );
  if (name === "nested") {
    assert.equal(
      llms.length,
      evidence.modelRequests,
      "nested source generations lost",
    );
    assert.equal(
      llms.reduce((n, s) => n + Number(a(s)["gen_ai.usage.output_tokens"]), 0),
      evidence.modelRequests * 11,
    );
    assert.equal(new Set(spans.map((s) => s.spanId)).size, spans.length);
    const agentTools = spans.filter((s) => ["Agent", "Task"].includes(s.name));
    assert(agentTools.length >= 2, "nested Agent tool missing");
    for (const tool of agentTools)
      assert(
        llms.some((s) => s.parentSpanId === tool.spanId),
        "Agent has no child generations",
      );
  }
  if (["subagent", "nested"].includes(name)) {
    const tool = spans.find((s) => ["Agent", "Task"].includes(s.name));
    assert(tool, "missing Agent tool");
    const descendants = new Set([tool.spanId]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const s of spans)
        if (descendants.has(s.parentSpanId) && !descendants.has(s.spanId)) {
          descendants.add(s.spanId);
          changed = true;
        }
    }
    assert(
      llms.some((s) => descendants.has(s.spanId)),
      "no model nested under Agent",
    );
    for (const delegate of spans.filter((s) =>
      ["Agent", "Task"].includes(s.name),
    )) {
      const delegatedPrompt = JSON.parse(a(delegate)["input.value"]).prompt;
      assert.equal(typeof delegatedPrompt, "string");
      const childGenerations = llms.filter(
        (s) => s.parentSpanId === delegate.spanId,
      );
      assert(childGenerations.length > 0);
      for (const generation of childGenerations) {
        const history = JSON.parse(a(generation)["input.value"]);
        assert(
          history.some(
            (m) => m.role === "user" && m.content === delegatedPrompt,
          ),
          `source delegated prompt missing from child generation ${generation.spanId}`,
        );
      }
    }
  }
}
(async () => {
  await fs.mkdir(out, { recursive: true });
  await fs.mkdir("/work/sample", { recursive: true });
  await fs.writeFile("/work/sample/sample.txt", "TOOL_OUTPUT_SAMPLE\n");
  const version = execFileSync(binary, ["--version"], {
    encoding: "utf8",
  }).trim();
  assert(version.startsWith(adapter.TESTED_HOST));
  console.log(
    JSON.stringify({
      version,
      network: "isolated network namespace: loopback only",
    }),
  );
  await listen(model);
  await listen(capture);
  target = `http://127.0.0.1:${capture.address().port}/v1/traces`;
  try {
    const errors = [];
    for (const [name, options] of [
      ["hooks", {}],
      ["stop-block", {}],
      ["multi", { multi: true }],
      ["interrupt-recover", { multi: true }],
      ["interrupt-tool", { multi: true }],
      ["host-hooks-disabled", {}],
      ["private", { privacy: false }],
      ["settings-precedence", { settings: true }],
      ["disabled", { disabled: true }],
      ["tool-error", {}],
      ["provider-error", {}],
      ["subagent", {}],
      ["nested", {}],
      ["project-private", {}],
      ["repo-endpoint-attack", {}],
      ["export-outage", {}],
    ])
      try {
        await run(name, options);
      } catch (e) {
        errors.push(name + ": " + e.message);
      }
    assert.deepEqual(errors, []);
  } finally {
    await fs.writeFile(
      out + "/exports.json",
      JSON.stringify(captures, null, 2),
    );
    await fs.writeFile(
      out + "/requests.json",
      JSON.stringify(requests, null, 2),
    );
    model.close();
    capture.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
  model.close();
  capture.close();
});
