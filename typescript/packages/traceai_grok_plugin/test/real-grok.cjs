// Run only in the secret-free E2B harness with packed packages and the pinned real host.
const assert = require("node:assert/strict"),
  fs = require("node:fs/promises"),
  path = require("node:path"),
  http = require("node:http"),
  cp = require("node:child_process");
const runtime = "/work/installed/node_modules",
  core = require(runtime + "/@traceai/agent-core"),
  plugin = require(runtime + "/@traceai/grok-plugin");
const home = "/work/grok-home",
  project = "/work/sample-project",
  bin = "/work/host/node_modules/.bin/grok";
const captures = [],
  requests = [],
  results = [];
let mode = "success",
  modelCalls = 0;
function run(args, name, extra = {}) {
  return new Promise((resolve, reject) => {
    const p = cp.spawn(bin, ["--no-auto-update", ...args], {
      cwd: project,
      env: { ...process.env, ...extra },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let so = "",
      se = "";
    p.stdout.on("data", (b) => (so += b));
    p.stderr.on("data", (b) => (se += b));
    const timer = setTimeout(() => p.kill("SIGTERM"), 30000);
    p.on("error", reject);
    p.on("exit", async (code, signal) => {
      clearTimeout(timer);
      await fs.writeFile("/work/evidence/" + name + ".stdout", so);
      await fs.writeFile("/work/evidence/" + name + ".stderr", se);
      results.push({ name, code, signal });
      resolve({ code, so, se });
    });
    if (mode === "cancel" && name === "cancel")
      setTimeout(() => p.kill("SIGINT"), 1500);
  });
}
const server = http.createServer((q, r) => {
  let b = "";
  q.on("data", (c) => (b += c));
  q.on("end", () => {
    try {
      const request = JSON.parse(b);
      if (q.url === "/tracer/v1/traces") {
        captures.push(request);
        r.setHeader("Content-Type", "application/json");
        r.end("{}");
        return;
      }
      requests.push({
        url: q.url,
        model: request.model,
        stream: request.stream,
        mode,
      });
      modelCalls++;
      if (mode === "cancel") return;
      const last = request.messages?.at(-1),
        hasTool = last?.role === "tool";
      let content = "Synthetic final response.",
        tool;
      if (!hasTool) {
        if (mode === "subagent")
          tool = {
            id: "call_spawn",
            type: "function",
            function: {
              name: "spawn_subagent",
              arguments: JSON.stringify({
                description: "Read synthetic sample",
                prompt:
                  "Read /work/sample-project/sample.txt and report its first word.",
                background: false,
              }),
            },
          };
        else
          tool = {
            id: "call_read_" + modelCalls,
            type: "function",
            function:
              mode === "error"
                ? {
                    name: "run_terminal_command",
                    arguments: JSON.stringify({
                      command: "exit 7",
                      description: "Exercise a synthetic tool failure",
                    }),
                  }
                : {
                    name: "read_file",
                    arguments: JSON.stringify({
                      target_file: "/work/sample-project/sample.txt",
                    }),
                  },
          };
      }
      // Child requests are recognized by their unique user message; keep the child mock finite.
      if (
        mode === "subagent" &&
        JSON.stringify(request.messages).includes("report its first word.")
      ) {
        tool = undefined;
        content = "Synthetic child response.";
      }
      const delta = tool
        ? { tool_calls: [{ index: 0, ...tool }] }
        : { content };
      if (request.stream) {
        r.writeHead(200, { "Content-Type": "text/event-stream" });
        r.write(
          "data: " +
            JSON.stringify({
              id: "chatcmpl-" + modelCalls,
              object: "chat.completion.chunk",
              created: 1,
              model: "test",
              choices: [{ index: 0, delta, finish_reason: null }],
            }) +
            "\n\n",
        );
        r.write(
          "data: " +
            JSON.stringify({
              id: "chatcmpl-" + modelCalls,
              object: "chat.completion.chunk",
              created: 1,
              model: "test",
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
              },
            }) +
            "\n\n",
        );
        r.end("data: [DONE]\n\n");
      } else {
        r.setHeader("Content-Type", "application/json");
        r.end(
          JSON.stringify({
            id: "chatcmpl-" + modelCalls,
            object: "chat.completion",
            created: 1,
            model: "test",
            choices: [
              {
                index: 0,
                message: {
                  role: "assistant",
                  content: tool ? null : content,
                  ...(tool ? { tool_calls: [tool] } : {}),
                },
                finish_reason: tool ? "tool_calls" : "stop",
              },
            ],
            usage: {
              prompt_tokens: 40,
              completion_tokens: 10,
              total_tokens: 50,
            },
          }),
        );
      }
    } catch (e) {
      r.writeHead(400);
      r.end("{}");
    }
  });
});
function spans() {
  return captures.flatMap((c) =>
    (c.resourceSpans ?? []).flatMap((r) =>
      r.scopeSpans.flatMap((s) => s.spans),
    ),
  );
}
function attrs(span) {
  return Object.fromEntries(
    span.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
server.listen(0, "127.0.0.1", async () => {
  try {
    await fs.mkdir("/work/evidence", { recursive: true });
    await fs.mkdir(project, { recursive: true, mode: 0o700 });
    await fs.mkdir(home, { recursive: true, mode: 0o700 });
    await fs.mkdir(home + "/.grok", { recursive: true, mode: 0o700 });
    await fs.writeFile(
      project + "/sample.txt",
      "Synthetic sample for Grok tracing.",
    );
    Object.assign(process.env, {
      HOME: home,
      FI_API_KEY: "dummy-fagi-api",
      FI_SECRET_KEY: "dummy-fagi-secret",
      FI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
      MOCK_API_KEY: "mock-only",
      PATH: "/work/host/node_modules/.bin:" + process.env.PATH,
      LD_PRELOAD: "/work/loopback-only.so",
    });
    await fs.writeFile(
      home + "/.grok/config.toml",
      `[model.mock]\nmodel = "test"\nbase_url = "http://127.0.0.1:${server.address().port}/v1"\nname = "Mock"\nenv_key = "MOCK_API_KEY"\n[models]\ndefault = "mock"\n`,
      { mode: 0o600 },
    );
    const configured = await core.setup({
      agent: "grok",
      projectName: "grok-e2b",
      configureOnly: true,
    });
    assert.equal(configured.configured, true);
    const config = await core.resolveConfig({
      agent: "grok",
      projectDir: project,
    });
    const context = { config, scope: "user", projectDir: project };
    const held = { code: "DAEMON_FREE" };
    assert.equal(plugin.grokAdapter.requiresWorker, false);
    assert.equal((await plugin.grokAdapter.detect(context)).supported, true);
    const source = await plugin.materializePlugin(
      context,
      runtime + "/@traceai/grok-plugin",
    );
    await plugin.materializePlugin(context, runtime + "/@traceai/grok-plugin");
    const noTrust = await run(["plugin", "install", source], "trust-required");
    assert.notEqual(noTrust.code, 0);
    // Explicit test-only approval of this synthetic fixture plugin. Production adapter never passes --trust.
    assert.equal(
      (await run(["plugin", "install", source, "--trust"], "approved-fixture"))
        .code,
      0,
    );
    await run(["plugin", "list", "--json"], "plugin-list");
    await run(["inspect"], "inspect");
    assert.equal((await plugin.grokAdapter.inspect(context)).registered, true);
    const common = [
      "-m",
      "mock",
      "--output-format",
      "streaming-json",
      "--allow",
      "read_file",
      "--allow",
      "run_terminal_command",
      "--max-turns",
      "4",
    ];
    const sid = "11111111-1111-4111-8111-111111111111";
    assert.equal(
      (
        await run(
          [
            "-p",
            "Read sample.txt and summarize.",
            "--session-id",
            sid,
            ...common,
          ],
          "success",
        )
      ).code,
      0,
    );
    await wait(1500);
    const first = spans().slice();
    assert.ok(
      first.length >= 3,
      "root, tool and reconstructed assistant messages must arrive",
    );
    mode = "error";
    assert.equal(
      (
        await run(
          ["-p", "Read missing.txt.", "--resume", sid, ...common],
          "error-resume",
        )
      ).code,
      0,
    );
    await wait(1500);
    const afterError = spans().slice();
    assert.ok(
      afterError.some(
        (s) => attrs(s)["fi.span.kind"] === "TOOL" && s.status.code === 2,
      ),
      "tool error observed",
    );
    mode = "subagent";
    await run(
      [
        "-p",
        "Delegate a short read-only sample check to a subagent.",
        ...common,
        "--allow",
        "Task",
      ],
      "subagent",
    );
    await wait(1500);
    mode = "cancel";
    const cancelled = await run(
      ["-p", "Wait for a slow response.", ...common],
      "cancel",
    );
    await wait(1500);
    const beforeDisable = spans().length;
    await core.disable({ agent: "grok" });
    mode = "success";
    await run(
      ["-p", "Read sample.txt after tracing is disabled.", ...common],
      "disabled",
    );
    await wait(1500);
    assert.equal(spans().length, beforeDisable);
    async function snapshot(dir, relative = "") {
      for (const item of await fs.readdir(dir, { withFileTypes: true })) {
        const p = path.join(dir, item.name),
          rel = path.join(relative, item.name);
        if (item.isDirectory()) await snapshot(p, rel);
        else if (["updates.jsonl", "events.jsonl"].includes(item.name)) {
          await fs.mkdir("/work/evidence/source/" + path.dirname(rel), {
            recursive: true,
          });
          await fs.copyFile(p, "/work/evidence/source/" + rel);
        }
      }
    }
    await snapshot(home + "/.grok/sessions");
    await fs.cp(
      config.paths.configDir + "/grok-state",
      "/work/evidence/structural-state",
      { recursive: true },
    );
    const all = spans();
    const roots = all.filter((s) => !s.parentSpanId),
      attributes = roots.map(attrs);
    assert.equal(
      roots.filter(
        (s) => attrs(s)["session.id"] === attributes[0]["session.id"],
      ).length,
      2,
      "two resumed user prompts share a session",
    );
    assert.equal(roots.length, 4, "exactly one root per enabled user prompt");
    const generations = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
    assert.ok(generations.length >= 6, "source assistant messages render as generations");
    assert.ok(generations.every((s) => attrs(s)["input.value"]), "generation inputs are present");
    for (const root of roots) {
      const trace = all.filter((s) => s.traceId === root.traceId);
      const owners = trace.filter((s) => attrs(s)["gen_ai.usage.total_tokens"] !== undefined);
      if (attrs(root)["fi.agent.summary.input"] !== undefined) {
        assert.equal(owners.length, 1, "one additive usage owner per completed turn");
        assert.equal(attrs(owners[0])["fi.span.kind"], "LLM");
        assert.equal(attrs(root)["gen_ai.usage.total_tokens"], undefined);
      }
    }
    assert.ok(all.filter((s) => attrs(s)["fi.span.kind"] === "TOOL").every((s) => attrs(s)["tool.name"] && attrs(s)["tool_call.id"]));
    assert.ok(
      roots.some((s) => attrs(s)["fi.agent.summary.input"] === 80),
      "turn usage is read after terminal hook",
    );
    for (const c of captures)
      for (const rs of c.resourceSpans ?? [])
        assert.equal(
          rs.resource.attributes.find((a) => a.key === "project_type").value
            .stringValue,
          "observe",
        );
    await fs.writeFile(
      "/work/evidence/spans.json",
      JSON.stringify(all, null, 2),
    );
    await fs.writeFile(
      "/work/evidence/result.json",
      JSON.stringify(
        {
          host: "1.0.77",
          requests,
          results,
          spans: all.map((s) => ({
            name: s.name,
            traceId: s.traceId,
            spanId: s.spanId,
            parentSpanId: s.parentSpanId,
            status: s.status,
            attributes: attrs(s),
          })),
          coreSetup: held.code,
          noTrustCode: noTrust.code,
          afterDisable: spans().length,
        },
        null,
        2,
      ),
    );
    assert.ok(
      all.some((s) => s.name === "subagent"),
      "real nested subagent span must arrive",
    );
    // Pinned upstream gap: SIGINT omits StopCancelled, turn_completed AND turn_ended.
    // Preserve incomplete, never infer successful completion or invent an interruption event.
    assert.equal(results.find((r) => r.name === "cancel").code, 130);
    const cancelledEnd = cancelled.so
      .trim()
      .split("\n")
      .map(JSON.parse)
      .find((r) => r.type === "end");
    const cancelDir = path.join(
      home,
      ".grok/sessions",
      encodeURIComponent(project),
      cancelledEnd.sessionId,
    );
    assert.doesNotMatch(
      await fs.readFile(path.join(cancelDir, "updates.jsonl"), "utf8"),
      /"(?:stop_cancelled|turn_completed)"/,
    );
    assert.doesNotMatch(
      await fs.readFile(path.join(cancelDir, "events.jsonl"), "utf8"),
      /"turn_ended"/,
    );
    assert.ok(
      all.some(
        (s) =>
          attrs(s)["fi.agent.outcome"] === "incomplete" &&
          attrs(s)["input.value"] === "Wait for a slow response.",
      ),
      "missing upstream terminal is explicitly incomplete",
    );
    await fs.writeFile(
      "/work/evidence/qualification.json",
      JSON.stringify(
        {
          status: "PASS_HEADLESS",
          limitations: [
            "Grok 1.0.77 headless SIGINT omits StopCancelled and persisted turn-end records; only incomplete is source-supported.",
            "Stop executes before finalized usage and Grok ignores async hooks; interactive export waits for idle Notification or SessionEnd. Interactive reload/compaction and real product media remain unqualified.",
          ],
        },
        null,
        2,
      ),
    );
    async function metadataOnly(dir) {
      for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name);
        if (entry.isDirectory()) await metadataOnly(p);
        else if (entry.name.endsWith(".json")) {
          assert.equal((await fs.stat(p)).mode & 0o777, 0o600);
          const state = JSON.parse(await fs.readFile(p, "utf8"));
          for (const e of state.events ?? []) {
            assert.equal(e.input, undefined);
            assert.equal(e.output, undefined);
            assert.equal(e.cwd, undefined);
          }
        }
      }
    }
    await metadataOnly(config.paths.configDir + "/grok-state");
    const diagnostic = await core.inspectStatus({
      agent: "grok",
      adapter: plugin.grokAdapter,
      check: true,
    });
    assert.equal(diagnostic.enabled, false);
    assert.equal(diagnostic.credentialCheck, "ACCEPTED");
    assert.doesNotMatch(
      JSON.stringify(diagnostic),
      /dummy-fagi-api|dummy-fagi-secret/,
    );
    await fs.mkdir("/work/unrelated/.grok-plugin", { recursive: true });
    await fs.writeFile(
      "/work/unrelated/.grok-plugin/plugin.json",
      JSON.stringify({ name: "unrelated-fixture", version: "1.0.0" }),
    );
    await run(
      ["plugin", "install", "/work/unrelated", "--trust"],
      "unrelated-install",
    );
    await fs.rename(runtime + "/@traceai/grok-plugin/dist/hook.js", runtime + "/@traceai/grok-plugin/dist/hook.missing");
    assert.equal((await plugin.grokAdapter.inspect(context)).detail, "RUNTIME_MISSING_OR_CHANGED");
    assert.equal(
      (await plugin.grokAdapter.uninstall(context)).registered,
      false,
    );
    assert.match(
      (await run(["plugin", "list", "--json"], "after-uninstall")).so,
      /unrelated-fixture/,
    );
    console.log(
      "REAL_GROK_EVIDENCE",
      JSON.stringify({
        roots: roots.length,
        spans: all.length,
        requests: requests.length,
        subagents: all.filter((s) => s.name === "subagent").length,
        interrupted: all.filter(
          (s) => attrs(s)["fi.agent.outcome"] === "interrupted",
        ).length,
        setupHold: held.code,
      }),
    );
  } catch (e) {
    console.error(e);
    process.exitCode = 1;
    await fs
      .writeFile(
        "/work/evidence/failure.json",
        JSON.stringify(
          { error: e.message, results, requests, captures },
          null,
          2,
        ),
      )
      .catch(() => {});
  } finally {
    await fs
      .cp(
        home + "/.config/futureagi/agents/grok-state",
        "/work/evidence/structural-state",
        { recursive: true },
      )
      .catch(() => {});
    await fs
      .cp(home + "/.local/state/futureagi/agents", "/work/evidence/delivery", {
        recursive: true,
      })
      .catch(() => {});
    async function sources(dir, rel = "") {
      for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name),
          r = path.join(rel, entry.name);
        if (entry.isDirectory()) await sources(p, r);
        else if (["updates.jsonl", "events.jsonl"].includes(entry.name)) {
          await fs.mkdir("/work/evidence/source/" + path.dirname(r), {
            recursive: true,
          });
          await fs.copyFile(p, "/work/evidence/source/" + r);
        }
      }
    }
    await sources(home + "/.grok/sessions").catch(() => {});
    server.closeAllConnections();
    server.close();
  }
});
