// E2B only: actual pinned Codex + dummy Responses provider + local OTLP capture.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises"),
  path = require("node:path"),
  http = require("node:http"),
  { spawn } = require("node:child_process");
const packageDir =
  process.env.FI_TEST_PACKAGE_DIR ?? path.resolve(__dirname, "..");
const { listHooks, PLUGIN_ID, codexAdapter } = require(
  path.join(packageDir, "dist/index.js"),
);
const { resolveConfig } = require("node:module").createRequire(
  packageDir + "/package.json",
)("@traceai/agent-core");
const base = process.env.FI_TEST_EVIDENCE_DIR ?? "/work/evidence";
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const nested = process.env.FI_TEST_NESTED === "true";
const nestedReuse = process.env.FI_TEST_NESTED_REUSE === "true";
const longTurn = process.env.FI_TEST_LONG_TURN === "true";
const privateMarker = "native-synthetic-private-marker";
const fixtureMessages = (inputs) =>
  inputs
    .filter((i) => i.role === "user")
    .flatMap((i) =>
      typeof i.content === "string"
        ? [i.content]
        : (i.content ?? []).map((c) => c.text ?? ""),
    )
    .filter((text) =>
      /^(CHILD_FIXTURE|GRAND_FIXTURE)(:| SECOND:)/.test(text.trim()),
    );
const nestedSteps = { root: 0, child: 0 };
let liveProcess;
async function run(bin, args, env = process.env, input) {
  return new Promise((resolve, reject) => {
    const p = spawn(bin, args, { env, cwd: base });
    liveProcess = p;
    p.stdin.end(input);
    let out = "",
      err = "";
    const timer = setTimeout(() => {
      p.kill("SIGKILL");
      resolve({ code: 124, out, err: err + "\nTIMED_OUT" });
    }, 90000);
    p.stdout.on("data", (b) => (out += b));
    p.stderr.on("data", (b) => (err += b));
    p.on("error", reject);
    p.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ code, out, err });
    });
  });
}
async function main() {
  await fs.mkdir(base, { recursive: true, mode: 0o700 });
  const home = base + "/home";
  await fs.mkdir(home, { mode: 0o700 });
  await fs.mkdir(home + "/.codex", { mode: 0o700 });
  Object.assign(process.env, {
    HOME: home,
    CODEX_HOME: home + "/.codex",
    XDG_CONFIG_HOME: home + "/.config",
    XDG_DATA_HOME: home + "/.local/share",
    XDG_STATE_HOME: home + "/.local/state",
    MOCK_API_KEY: "dummy-model-key",
    FI_AGENT_DEBUG: "true",
  });
  const exports = [],
    requests = [];
  let count = 0,
    mode = "normal",
    parentStep = 0;
  const server = http.createServer(async (req, res) => {
    await fs.appendFile(
      base + "/requests.jsonl",
      JSON.stringify({ url: req.url }) + "\n",
    );
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let bytes = Buffer.concat(chunks);
    if (req.headers["content-encoding"] === "gzip")
      bytes = require("node:zlib").gunzipSync(bytes);
    const body = bytes.length ? JSON.parse(bytes) : {};
    if (req.url === "/tracer/v1/traces") {
      exports.push(body);
      res.setHeader("content-type", "application/json");
      res.end("{}");
      return;
    }
    if (!req.url.startsWith("/v1/responses")) {
      res.writeHead(404);
      res.end("{}");
      return;
    }
    requests.push(body);
    count++;
    if (mode === "provider-error") {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          error: {
            message: "synthetic provider rejection",
            type: "invalid_request_error",
          },
        }),
      );
      return;
    }
    if (mode === "interrupt") {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(
        "event: response.created\ndata: " +
          JSON.stringify({
            type: "response.created",
            response: {
              id: "resp_interrupt",
              object: "response",
              model: "gpt-5.1-codex",
              status: "in_progress",
              output: [],
            },
          }) +
          "\n\n",
      );
      const target = liveProcess;
      setTimeout(() => target?.kill("SIGINT"), 500);
      setTimeout(() => res.end(), 3000);
      return;
    }
    const tools = body.tools ?? [];
    const shell = tools.find((t) =>
      ["shell_command", "exec_command", "shell"].includes(t.name),
    );
    let tool = count <= (longTurn ? 40 : 1) && shell;
    // The real shell returns escaped JSON inside ordinary command output.
    const encoded = Buffer.from(
      JSON.stringify({
        payload: `PASSWORD="${privateMarker}"`,
        nested: JSON.stringify({ password: privateMarker }),
      }) + "\n",
    ).toString("base64");
    const command = longTurn
      ? `printf step-${count}`
      : `printf '%s' '${encoded}' | base64 -d; exit 7`;
    const args =
      shell?.name === "exec_command"
        ? { cmd: command, max_output_tokens: longTurn ? 20000 : 1000 }
        : shell?.name === "shell"
          ? { command: ["sh", "-c", command] }
          : { command };
    let item = tool
      ? {
          id: "fc_test_" + count,
          type: "function_call",
          call_id: "call_test_" + count,
          name: shell.name,
          arguments: JSON.stringify(args),
          status: "completed",
        }
      : {
          id: "msg_" + count,
          type: "message",
          role: "assistant",
          status: "completed",
          content: [
            {
              type: "output_text",
              text: "Codex tracing test completed.",
              annotations: [],
            },
          ],
        };
    if (mode === "subagent") {
      await fs.writeFile(
        base + "/subagent-request-" + count + ".json",
        JSON.stringify(body, null, 2),
      );
      const inputs = Array.isArray(body.input) ? body.input : [];
      const lastUser = inputs.filter((i) => i.role === "user").at(-1);
      if (nested) {
        const text = fixtureMessages(inputs).join("\n");
        const lane = text.includes("GRAND_FIXTURE")
          ? "grand"
          : text.includes("CHILD_FIXTURE")
            ? "child"
            : "root";
        if (
          lane !== "grand" &&
          nestedSteps[lane]++ < (nestedReuse && lane === "child" ? 4 : 2)
        ) {
          const step = nestedSteps[lane];
          const results = inputs
            .filter((i) => i.type === "function_call_output")
            .map((i) => i.output)
            .join("\n");
          const child = results.match(/"agent_id"\s*:\s*"([^"]+)"/)?.[1];
          item = {
            id: "nested_" + count,
            type: "function_call",
            namespace: "multi_agent_v1",
            name:
              step === 1
                ? "spawn_agent"
                : step === 3
                  ? "send_input"
                  : "wait_agent",
            call_id: "nested_call_" + count,
            arguments: JSON.stringify(
              step === 1
                ? {
                    message:
                      lane === "root"
                        ? "CHILD_FIXTURE: spawn grandchild then wait."
                        : "GRAND_FIXTURE: reply done.",
                    agent_type: "default",
                  }
                : step === 3
                  ? {
                      target: child,
                      message: "GRAND_FIXTURE SECOND: reply again.",
                    }
                  : { targets: [child], timeout_ms: 10000 },
            ),
            status: "completed",
          };
          tool = true;
        } else if (lane === "grand") {
          item.content[0].text = "GRANDCHILD_PROBE_COMPLETE";
        }
      } else {
        const isChild = JSON.stringify(lastUser ?? "").includes(
          "CHILD_FIXTURE",
        );
        if (!isChild && parentStep++ < 4) {
          const results = inputs
            .filter((i) => i.type === "function_call_output")
            .map((i) => i.output)
            .join("\n");
          const child = results.match(/"agent_id"\s*:\s*"([^"]+)"/)?.[1];
          item = {
            id: "fc_child_" + count,
            type: "function_call",
            namespace: "multi_agent_v1",
            name:
              parentStep === 3
                ? "send_input"
                : child
                  ? "wait_agent"
                  : "spawn_agent",
            call_id: "child_call_" + count,
            arguments: JSON.stringify(
              parentStep === 3
                ? {
                    target: child,
                    message: "CHILD_FIXTURE SECOND: Reply done again.",
                  }
                : child
                  ? { targets: [child], timeout_ms: 10000 }
                  : {
                      message: "CHILD_FIXTURE: Reply done.",
                      agent_type: "default",
                    },
            ),
            status: "completed",
          };
          tool = true;
        }
      }
    }

    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
    });
    const emit = (type, data) =>
      res.write(
        `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`,
      );
    const response = {
      id: "resp_" + count,
      object: "response",
      created_at: 1,
      model: "gpt-5.1-codex",
      status: "in_progress",
      output: [],
    };
    emit("response.created", { response });
    emit("response.output_item.added", {
      output_index: 0,
      item: { ...item, status: "in_progress" },
    });
    if (!tool) {
      emit("response.content_part.added", {
        item_id: item.id,
        output_index: 0,
        content_index: 0,
        part: { type: "output_text", text: "", annotations: [] },
      });
      emit("response.output_text.delta", {
        item_id: item.id,
        output_index: 0,
        content_index: 0,
        delta: item.content[0].text,
      });
      emit("response.output_text.done", {
        item_id: item.id,
        output_index: 0,
        content_index: 0,
        text: item.content[0].text,
      });
    }
    emit("response.output_item.done", { output_index: 0, item });
    emit("response.completed", {
      response: {
        ...response,
        status: "completed",
        output: [item],
        usage: {
          input_tokens: 20,
          output_tokens: 8,
          total_tokens: 28,
          input_tokens_details: { cached_tokens: 4 },
          output_tokens_details: { reasoning_tokens: 2 },
        },
      },
    });
    res.end();
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const port = server.address().port;
  process.env.FI_API_KEY = "dummy-fi-api";
  process.env.FI_SECRET_KEY = "dummy-fi-secret";
  process.env.FI_OTLP_ENDPOINT = `http://127.0.0.1:${port}/tracer/v1/traces`;
  const config = `model = "gpt-5.1-codex"\nmodel_provider = "mock"\n[model_providers.mock]\nname = "Local Responses"\nbase_url = "http://127.0.0.1:${port}/v1"\nwire_api = "responses"\nenv_key = "MOCK_API_KEY"\nsupports_websockets = false\n[analytics]\nenabled = false\n[feedback]\nenabled = false\n`;
  await fs.writeFile(home + "/.codex/config.toml", config, { mode: 0o600 });
  const cli =
    process.env.FI_TEST_CLI ??
    path.resolve(__dirname, "../../traceai_agents/dist/cli.js");
  const setup = await run(process.execPath, [
    cli,
    "setup",
    "codex",
    "--project",
    "codex-e2b",
    "--json",
  ]);
  await fs.writeFile(base + "/setup.json", JSON.stringify(setup, null, 2));
  assert.equal(setup.code, 2, setup.out + setup.err);
  assert.equal(JSON.parse(setup.out).code, "TRUST_REQUIRED");
  const context = {
    config: await resolveConfig({ agent: "codex", projectDir: base }),
    scope: "user",
    projectDir: base,
  };
  const installed = await codexAdapter.install(context);
  await fs.writeFile(base + "/installed.json", JSON.stringify(installed));
  assert(installed.registered);
  assert.equal(installed.trust, "required");
  let hooks = (await listHooks(base)).filter((h) => h.pluginId === PLUGIN_ID);
  await fs.writeFile(
    base + "/pending-hooks.json",
    JSON.stringify(hooks, null, 2),
  );
  assert.equal(hooks.length, 13);
  assert(hooks.every((h) => h.trustStatus === "untrusted"));
  const repeat = await codexAdapter.install(context);
  assert(repeat.registered);
  // An isolated APPROVED FIXTURE: copy only the hashes of the exact generated hooks.
  // Production installer has no trust write code; never use bypass_hook_trust.
  const review = hooks
    .map((h) => {
      const [declaration, content] = h.currentHash.split("/");
      assert(content);
      return `[hooks.state.${JSON.stringify(h.key)}]\ntrusted_hash = ${JSON.stringify(declaration)}\ntrusted_content_hash = ${JSON.stringify(content)}\n`;
    })
    .join("\n");
  await fs.appendFile(home + "/.codex/config.toml", "\n" + review, {
    mode: 0o600,
  });
  hooks = (await listHooks(base)).filter((h) => h.pluginId === PLUGIN_ID);
  assert(hooks.every((h) => h.trustStatus === "trusted"));
  await fs.writeFile(
    base + "/plugin-list.json",
    JSON.stringify(
      await run("codex", [
        "plugin",
        "list",
        "--json",
        "--marketplace",
        "futureagi-local",
      ]),
    ),
  );
  const owner = JSON.parse(
    await fs.readFile(
      context.config.paths.configDir + "/codex-installation.json",
      "utf8",
    ),
  );
  const runtime = owner.root + "/plugin/hook.cjs";
  const originalRuntime = await fs.readFile(runtime);
  await fs.unlink(runtime);
  assert.equal(
    (await codexAdapter.inspect(context)).detail,
    "HOOK_RUNTIME_MISSING",
  );
  for (const command of ["status", "doctor"]) {
    const result = await run(process.execPath, [
      cli,
      command,
      "codex",
      "--json",
    ]);
    const status = JSON.parse(result.out);
    assert.equal(result.code, 2);
    assert.equal(status.registration, "NOT_REGISTERED");
    assert(status.issues.includes("HOOK_RUNTIME_MISSING"));
    assert(status.recovery.includes("npx @traceai/agents setup codex"));
    assert.notEqual(status.trust, "confirmed");
  }
  assert(
    (await codexAdapter.install(context)).registered,
    "setup restores a missing runtime",
  );
  assert.equal((await codexAdapter.inspect(context)).trust, "confirmed");
  await fs.writeFile(runtime, "// changed runtime", { mode: 0o600 });
  assert.equal(
    (await codexAdapter.inspect(context)).detail,
    "HOOK_RUNTIME_CHANGED",
  );
  await fs.unlink(runtime);
  await fs.symlink(path.join(packageDir, "dist/hook.cjs"), runtime);
  assert.equal(
    (await codexAdapter.inspect(context)).detail,
    "HOOK_RUNTIME_CHANGED",
  );
  await fs.unlink(runtime);
  await fs.writeFile(runtime, originalRuntime, { mode: 0o600 });
  assert.equal((await codexAdapter.inspect(context)).trust, "confirmed");
  console.log(
    "NATIVE_RUNTIME_INTEGRITY",
    JSON.stringify({
      missing: true,
      changed: true,
      symlink: true,
      repaired: true,
    }),
  );
  const probe = await run(
    process.execPath,
    [owner.root + "/plugin/hook.cjs"],
    process.env,
    JSON.stringify({
      hook_event_name: "SessionStart",
      session_id: "probe",
      cwd: base,
    }),
  );
  await fs.writeFile(base + "/hook-probe.json", JSON.stringify(probe));
  const exec = await run("codex", [
    "-a",
    "never",
    "exec",
    "--skip-git-repo-check",
    "--sandbox",
    "workspace-write",
    "--json",
    "Run the shell test once, then report completion.",
  ]);
  await fs.writeFile(base + "/exec.json", JSON.stringify(exec, null, 2));
  assert.equal(exec.code, 0, exec.err);
  assert(
    !(exec.out + exec.err).includes("clamping"),
    "no invalid native hook timeout",
  );
  for (
    let i = 0;
    i < 50 && exports.flatMap((b) => b.resourceSpans ?? []).length === 0;
    i++
  )
    await delay(100);
  await delay(500);
  const spans = exports
    .flatMap((b) => b.resourceSpans ?? [])
    .flatMap((r) => r.scopeSpans ?? [])
    .flatMap((s) => s.spans ?? []);
  const attrs = (s) =>
    Object.fromEntries(
      s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
    );
  await fs.writeFile(base + "/otlp.json", JSON.stringify(exports, null, 2));
  const summary = {
    codex: "0.167.0",
    requests: requests.length,
    toolNames: requests[0]?.tools?.map((t) => t.name),
    spans: spans.map((s) => ({
      name: s.name,
      spanId: s.spanId,
      parentSpanId: s.parentSpanId,
      traceId: s.traceId,
      attributes: attrs(s),
    })),
  };
  await fs.writeFile(base + "/summary.json", JSON.stringify(summary, null, 2));
  console.log(
    "NATIVE_TURN",
    JSON.stringify({
      requests: requests.length,
      spans: spans.length,
      longTurn,
    }),
  );
  assert(
    !JSON.stringify(spans).includes(privateMarker),
    "no synthetic password in any exported span",
  );
  assert(requests.length >= 2, "real tool loop");
  assert.equal(
    spans.filter((s) => s.name === "codex").length,
    1,
    "one terminal root",
  );
  assert.equal(
    attrs(spans.find((s) => s.name === "codex"))["fi.agent.outcome"],
    "success",
    "authoritative successful final boundary",
  );
  assert.equal(
    spans.filter((s) => s.name === "codex.model_attempt").length,
    requests.length,
    "native per-attempt spans",
  );
  assert(
    spans.some((s) => attrs(s)["fi.span.kind"] === "TOOL"),
    "tool span",
  );
  const toolSpans = spans.filter((s) => attrs(s)["fi.span.kind"] === "TOOL");
  assert.equal(toolSpans.length, longTurn ? 40 : 1);
  assert(
    toolSpans.every((s) => attrs(s)["tool.name"] && attrs(s)["tool_call.id"]),
  );
  if (longTurn) {
    assert.equal(requests.length, 41);
    assert(toolSpans.every((s) => attrs(s)["fi.agent.outcome"] === "success"));
    assert.equal(
      spans.reduce(
        (sum, s) => sum + Number(attrs(s)["gen_ai.usage.input_tokens"] ?? 0),
        0,
      ),
      820,
    );
    assert.equal(
      spans.reduce(
        (sum, s) => sum + Number(attrs(s)["gen_ai.usage.output_tokens"] ?? 0),
        0,
      ),
      328,
    );
    console.log(
      "NATIVE_LONG_TURN",
      JSON.stringify({
        models: 41,
        tools: 40,
        input: 820,
        output: 328,
        outcome: "success",
      }),
    );
  } else {
    assert(
      toolSpans.some((s) => attrs(s)["fi.agent.outcome"] === "error"),
      "nonzero shell exit retained from rollout",
    );
  }
  assert.equal(
    spans.filter((s) => attrs(s)["gen_ai.usage.input_tokens"] !== undefined)
      .length,
    requests.length,
    "usage additive only on models",
  );
  const thread = exec.out
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l))
    .find((e) => e.type === "thread.started").thread_id;
  const resumed = await run("codex", [
    "-a",
    "never",
    "exec",
    "resume",
    "--skip-git-repo-check",
    "--json",
    thread,
    "Reply resumed.",
  ]);
  await fs.writeFile(base + "/resume.json", JSON.stringify(resumed, null, 2));
  assert.equal(resumed.code, 0, resumed.err);
  const allSpans = () =>
    exports
      .flatMap((b) => b.resourceSpans ?? [])
      .flatMap((r) => r.scopeSpans ?? [])
      .flatMap((s) => s.spans ?? []);
  const roots = allSpans().filter((s) => s.name === "codex");
  assert.equal(roots.length, 2);
  assert.equal(attrs(roots[0])["session.id"], attrs(roots[1])["session.id"]);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  await fs.writeFile(
    base + "/summary.json",
    JSON.stringify(
      {
        ...summary,
        requests: requests.length,
        spans: allSpans().map((s) => ({
          name: s.name,
          traceId: s.traceId,
          spanId: s.spanId,
          parentSpanId: s.parentSpanId,
          attributes: attrs(s),
        })),
        resumeVerified: true,
      },
      null,
      2,
    ),
  );
  mode = "subagent";
  const childRun = await run("codex", [
    ...(nested ? ["-c", "agents.max_depth=2"] : []),
    "-a",
    "never",
    "exec",
    "--skip-git-repo-check",
    "--json",
    "Run the subagent fixture.",
  ]);
  await fs.writeFile(
    base + "/subagent-exec.json",
    JSON.stringify(childRun, null, 2),
  );
  await fs.writeFile(base + "/otlp-all.json", JSON.stringify(exports, null, 2));
  assert.equal(childRun.code, 0, childRun.err);
  if (nested) {
    const grandRequests = requests.filter((r) =>
      fixtureMessages(r.input ?? []).some((text) =>
        text.startsWith("GRAND_FIXTURE"),
      ),
    );
    const nestedReport = {
      grandModelRequests: grandRequests.length,
      spans: allSpans().map((s) => ({
        name: s.name,
        traceId: s.traceId,
        spanId: s.spanId,
        parentSpanId: s.parentSpanId,
        attributes: attrs(s),
      })),
    };
    await fs.writeFile(
      base + "/nested-report.json",
      JSON.stringify(nestedReport, null, 2),
    );
    assert(grandRequests.length > 0, "real grandchild model ran");
    const grandGenerations = allSpans().filter(
      (s) =>
        s.name === "codex.model_attempt" &&
        (attrs(s)["input.value"] ?? "").includes("GRAND_FIXTURE"),
    );
    console.log(
      "NATIVE_NESTED_REVIEW",
      JSON.stringify({
        grandModelRequests: grandRequests.length,
        grandGenerations: grandGenerations.length,
        subagentSpans: allSpans().filter((s) => s.name === "codex.subagent")
          .length,
      }),
    );
    assert.equal(
      grandGenerations.length,
      nestedReuse ? 2 : 1,
      "every real grandchild execution captured",
    );
    const grandSpan = allSpans().find(
      (s) => s.spanId === grandGenerations[0].parentSpanId,
    );
    const childGeneration = allSpans().find(
      (s) =>
        s.name === "codex.model_attempt" &&
        (attrs(s)["input.value"] ?? "").includes("CHILD_FIXTURE"),
    );
    const childSpan = allSpans().find(
      (s) => s.spanId === childGeneration.parentSpanId,
    );
    const rootSpan = allSpans().find(
      (s) => s.name === "codex" && s.traceId === grandSpan.traceId,
    );
    assert.equal(childSpan.parentSpanId, rootSpan.spanId);
    assert.equal(
      grandSpan.parentSpanId,
      childSpan.spanId,
      "grandchild belongs under the spawning child",
    );
    assert.equal(grandRequests.length, nestedReuse ? 2 : 1);
    assert.equal(
      allSpans().filter((s) => s.name === "codex").length,
      3,
      "reuse creates no extra prompt root",
    );
    for (const generation of grandGenerations) {
      const execution = allSpans().find(
        (s) => s.spanId === generation.parentSpanId,
      );
      assert.equal(generation.traceId, rootSpan.traceId);
      assert.equal(
        execution.parentSpanId,
        childSpan.spanId,
        "reused grandchild stays under its spawning child",
      );
      assert(
        attrs(generation)["output.value"].includes("GRANDCHILD_PROBE_COMPLETE"),
      );
    }
    console.log(
      "NATIVE_NESTED_ANCESTRY",
      JSON.stringify({
        root: rootSpan.spanId,
        child: childSpan.spanId,
        grandchild: grandSpan.spanId,
        actualParent: grandSpan.parentSpanId,
        expectedParent: childSpan.spanId,
      }),
    );
  } else {
    const childSpan = allSpans().find((s) => s.name === "codex.subagent");
    assert(childSpan, "real subagent lifecycle");
    assert.equal(
      allSpans().filter((s) => s.name === "codex").length,
      3,
      "a child must not create a fourth prompt root",
    );
    const parentRoot = allSpans().find(
      (s) =>
        s.name === "codex" &&
        attrs(s)["input.value"] === "Run the subagent fixture.",
    );
    assert.equal(
      childSpan.traceId,
      parentRoot.traceId,
      "child belongs to actual parent prompt",
    );
    assert(
      allSpans().some(
        (s) =>
          s.name === "codex.model_attempt" &&
          s.parentSpanId === childSpan.spanId &&
          s.traceId === childSpan.traceId,
      ),
      "nested child request",
    );
    const childParents = allSpans().filter(
      (s) => s.name === "codex.subagent" && s.traceId === parentRoot.traceId,
    );
    const childGenerations = allSpans().filter(
      (s) =>
        s.name === "codex.model_attempt" &&
        childParents.some((p) => p.spanId === s.parentSpanId),
    );
    assert.equal(
      childParents.length,
      2,
      "one nested span per child execution, including reuse",
    );
    assert.deepEqual(
      childGenerations.map((s) => attrs(s)["input.value"]).sort(),
      ["CHILD_FIXTURE SECOND: Reply done again.", "CHILD_FIXTURE: Reply done."],
    );
    assert(childGenerations.every((s) => s.traceId === parentRoot.traceId));
    assert.equal(attrs(parentRoot)["fi.agent.outcome"], "success");
    await fs.writeFile(
      base + "/reuse-report.json",
      JSON.stringify(
        {
          childGenerations: childGenerations.map((s) => ({
            parentSpanId: s.parentSpanId,
            attributes: attrs(s),
          })),
          nestedCount: childGenerations.length,
          childParents: childParents.map((s) => s.spanId),
        },
        null,
        2,
      ),
    );
  }
  mode = "interrupt";
  const interrupted = await run("codex", [
    "-a",
    "never",
    "exec",
    "--skip-git-repo-check",
    "--json",
    "Wait for the interrupt fixture.",
  ]);
  await fs.writeFile(
    base + "/interrupt-exec.json",
    JSON.stringify(interrupted, null, 2),
  );
  await fs.writeFile(
    base + "/otlp-interrupt.json",
    JSON.stringify(exports, null, 2),
  );
  assert(
    allSpans().some(
      (s) =>
        s.name === "codex" && attrs(s)["fi.agent.outcome"] === "interrupted",
    ),
    "native Interrupt finalizes interrupted root",
  );
  assert.equal(
    allSpans().filter((s) => s.name === "codex.model_attempt").length,
    requests.length,
    "include every native request, even queued child notification after Stop",
  );
  mode = "normal";
  await fs.writeFile(
    base + "/summary.json",
    JSON.stringify(
      {
        codex: "0.167.0",
        resumeVerified: true,
        subagentVerified: true,
        requests: requests.length,
        spans: allSpans().map((s) => ({
          name: s.name,
          traceId: s.traceId,
          spanId: s.spanId,
          parentSpanId: s.parentSpanId,
          attributes: attrs(s),
        })),
      },
      null,
      2,
    ),
  );
  mode = "provider-error";
  const beforeError = allSpans().length;
  process.env.FI_HIDE_ERROR_DETAILS = "true";
  const rejected = await run("codex", [
    "-a",
    "never",
    "exec",
    "--skip-git-repo-check",
    "--json",
    "Trigger the provider error fixture.",
  ]);
  for (
    let i = 0;
    i < 80 &&
    !allSpans()
      .slice(beforeError)
      .some((s) => s.name === "codex");
    i++
  )
    await delay(100);
  const failedSpans = allSpans().slice(beforeError);
  assert.equal(rejected.code, 1, "non-retryable provider rejection");
  for (const name of ["codex", "codex.model_attempt"]) {
    const span = failedSpans.find((s) => s.name === name);
    assert(span, `missing provider-error ${name}`);
    assert.equal(attrs(span)["fi.agent.outcome"], "error");
    assert.equal(span.status.code, 2);
  }
  assert(
    !JSON.stringify(failedSpans).includes("synthetic provider rejection"),
    "hide-error-details includes provider failure output",
  );
  await fs.writeFile(
    base + "/provider-error.json",
    JSON.stringify({ exitCode: rejected.code, spans: failedSpans }, null, 2),
  );
  console.log(
    "NATIVE_PROVIDER_ERROR",
    JSON.stringify({
      exitCode: rejected.code,
      spans: failedSpans.length,
      root: "error",
      model: "error",
      hiddenDetails: true,
    }),
  );
  delete process.env.FI_HIDE_ERROR_DETAILS;
  mode = "normal";
  const disable = await run(process.execPath, [
    cli,
    "disable",
    "codex",
    "--json",
  ]);
  assert.equal(disable.code, 0);
  const n = allSpans().length;
  await run("codex", [
    "-a",
    "never",
    "exec",
    "--skip-git-repo-check",
    "--json",
    "Reply done.",
  ]);
  await delay(500);
  assert.equal(
    exports
      .flatMap((b) => b.resourceSpans ?? [])
      .flatMap((r) => r.scopeSpans ?? [])
      .flatMap((s) => s.spans ?? []).length,
    n,
    "kill switch",
  );
  assert(
    allSpans()
      .filter((s) => attrs(s)["fi.span.kind"] === "TOOL")
      .every((s) => attrs(s)["tool.name"]),
    "backend tool names present",
  );
  const ownership = context.config.paths.configDir + "/codex-installation.json";
  await fs.writeFile(
    ownership,
    JSON.stringify({ ...owner, interpreter: base + "/missing-node" }),
    { mode: 0o600 },
  );
  assert.equal(
    (await codexAdapter.inspect(context)).detail,
    "NODE_INTERPRETER_MISSING",
  );
  await fs.writeFile(ownership, JSON.stringify(owner), { mode: 0o600 });
  // An old owned runtime is pruned only after replacement registration succeeds.
  const oldRoot = path.join(
    path.dirname(path.dirname(owner.root)),
    "0.0.0",
    path.basename(owner.root),
  );
  await fs.mkdir(path.dirname(oldRoot), { recursive: true, mode: 0o700 });
  await fs.cp(owner.root, oldRoot, { recursive: true });
  await fs.writeFile(ownership, JSON.stringify({ ...owner, root: oldRoot }), {
    mode: 0o600,
  });
  assert((await codexAdapter.install(context)).registered);
  await assert.rejects(fs.stat(oldRoot), { code: "ENOENT" });
  const marketplacePath = path.join(
    owner.root,
    ".agents/plugins/marketplace.json",
  );
  const originalMarketplace = await fs.readFile(marketplacePath, "utf8");
  const changedMarketplace = JSON.parse(originalMarketplace);
  changedMarketplace.plugins.push({
    name: "unrelated",
    source: "./another-plugin",
  });
  await fs.writeFile(marketplacePath, JSON.stringify(changedMarketplace));
  await assert.rejects(
    codexAdapter.uninstall(context),
    /INSTALLATION_OWNERSHIP_CHANGED/,
  );
  assert((await fs.stat(owner.root)).isDirectory());
  await fs.writeFile(marketplacePath, originalMarketplace);
  assert.equal((await codexAdapter.uninstall(context)).registered, false);
  await assert.rejects(fs.stat(owner.root), { code: "ENOENT" });
  server.close();
}
main()
  .then(() => process.exit(0))
  .catch((e) => {
    console.error(e);
    process.exit(1);
  });
