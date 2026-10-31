#!/usr/bin/env node
// Explicit opt-in only: real Claude binary, throwaway HOME, no login, loopback model and OTLP.
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");

async function run({
  binary,
  root,
  sandbox,
  packageRoot = path.resolve(__dirname, ".."),
  scenario = "success",
}) {
  assert.ok(["success", "failure", "cancel"].includes(scenario));
  if (
    process.platform !== "darwin" ||
    ![binary, root, sandbox, packageRoot].every(path.isAbsolute)
  )
    throw Error("ISOLATED_MAC_RUN_REQUIRED");
  const { decodeTraces, encodeResponse } = require(
    path.join(packageRoot, "dist/src/export/otlp.js"),
  );
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const home = fs.mkdtempSync(path.join(root, "r-"));
  const bin = path.join(home, "bin");
  fs.mkdirSync(bin, { mode: 0o700 });
  fs.symlinkSync(binary, path.join(bin, "claude"));
  const agent = path.join(home, "a");
  fs.mkdirSync(agent, { mode: 0o700 });
  fs.writeFileSync(
    path.join(home, "fixture.txt"),
    "PRIVACY_CANARY_TOOL_OUTPUT",
    { mode: 0o600 },
  );
  let calls = 0;
  let cancelChild;
  let cancellations = 0;
  const uploads = [];
  const server = http.createServer(async (req, res) => {
    let size = 0;
    const chunks = [];
    for await (const c of req) {
      size += c.length;
      if (size > 2 * 1024 * 1024) {
        res.writeHead(413).end();
        return;
      }
      chunks.push(c);
    }
    const bytes = Buffer.concat(chunks);
    if (req.url === "/v1/traces") {
      assert.equal(req.headers["x-api-key"], "synthetic-api");
      assert.equal(req.headers["x-secret-key"], "synthetic-secret");
      uploads.push(decodeTraces(bytes));
      res.setHeader("content-type", "application/x-protobuf");
      res.end(encodeResponse());
      return;
    }
    if (req.url.includes("count_tokens")) {
      res.setHeader("content-type", "application/json");
      res.end('{"input_tokens":1}');
      return;
    }
    if (!req.url.includes("/messages")) {
      res.writeHead(404).end();
      return;
    }
    const body = JSON.parse(bytes);
    calls++;
    if (scenario === "failure") {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          type: "error",
          error: {
            type: "invalid_request_error",
            message: "PRIVACY_CANARY_ERROR",
          },
        }),
      );
      return;
    }
    if (scenario === "cancel") {
      // Interrupt only after the real binary has begun its request. No model
      // response is sent; the OS signal, not a synthetic error, ends the run.
      assert.ok(cancelChild);
      const cancel = cancelChild;
      cancelChild = undefined;
      cancellations++;
      setImmediate(cancel);
      return;
    }
    const tool = calls === 1;
    const block = tool
      ? {
          type: "tool_use",
          id: "toolu_fixture_1",
          name: "Read",
          input: { file_path: path.join(home, "fixture.txt") },
        }
      : { type: "text", text: "fixture-ok" };
    const message = {
      id: "msg_fixture_" + calls,
      type: "message",
      role: "assistant",
      model: body.model,
      content: [block],
      stop_reason: tool ? "tool_use" : "end_turn",
      stop_sequence: null,
      usage: {
        input_tokens: 10,
        output_tokens: 2,
        cache_read_input_tokens: 7,
        cache_creation_input_tokens: 3,
      },
    };
    res.setHeader("request-id", "req_fixture_" + calls);
    if (!body.stream) {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify(message));
      return;
    }
    res.writeHead(200, { "content-type": "text/event-stream" });
    for (const e of [
      {
        type: "message_start",
        message: {
          ...message,
          content: [],
          stop_reason: null,
          usage: { ...message.usage, output_tokens: 0 },
        },
      },
      {
        type: "content_block_start",
        index: 0,
        content_block: tool
          ? { ...block, input: {} }
          : { type: "text", text: "" },
      },
      {
        type: "content_block_delta",
        index: 0,
        delta: tool
          ? {
              type: "input_json_delta",
              partial_json: JSON.stringify(block.input),
            }
          : { type: "text_delta", text: "fixture-ok" },
      },
      { type: "content_block_stop", index: 0 },
      {
        type: "message_delta",
        delta: { stop_reason: message.stop_reason, stop_sequence: null },
        usage: { output_tokens: 2 },
      },
      { type: "message_stop" },
    ])
      res.write("event: " + e.type + "\ndata: " + JSON.stringify(e) + "\n\n");
    res.end();
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const origin = "http://127.0.0.1:" + server.address().port;
  const manifest = path.join(home, "contract.json");
  fs.writeFileSync(
    manifest,
    JSON.stringify({
      contract: "immutable-agent-v1",
      endpoint: origin + "/v1/traces",
      project: "fixture",
      immutableAdmission: true,
      uniqueReads: true,
      usagePlans: true,
    }),
    { mode: 0o600 },
  );
  const env = {
    PATH: bin + ":" + path.dirname(process.execPath) + ":/usr/bin:/bin",
    HOME: home,
    TMPDIR: home,
    CLAUDE_CONFIG_DIR: agent,
    FI_AGENT_CONFIG_HOME: path.join(home, "c"),
    FI_AGENT_STATE_HOME: path.join(home, "s"),
    ANTHROPIC_BASE_URL: origin,
    ANTHROPIC_API_KEY: "synthetic-model-key",
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    DISABLE_AUTOUPDATER: "1",
    NO_PROXY: "127.0.0.1,localhost",
  };
  const cli = path.join(packageRoot, "bin/fi-agent.cjs");
  async function exec(executable, args, input = "", interrupt = false) {
    return new Promise((resolve, reject) => {
      let stdout = "",
        count = 0;
      const child = spawn(
        "/usr/bin/sandbox-exec",
        ["-f", sandbox, executable, ...args],
        { cwd: home, env, stdio: ["pipe", "pipe", "pipe"], detached: true },
      );
      if (interrupt) cancelChild = () => child.kill("SIGINT");
      const kill = () => {
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch {
          child.kill("SIGKILL");
        }
      };
      const timer = setTimeout(kill, 60000);
      child.stdin.end(input);
      child.stdout.on("data", (c) => {
        count += c.length;
        if (count > 2 * 1024 * 1024) kill();
        else stdout += c;
      });
      child.stderr.on("data", (c) => {
        count += c.length;
        if (count > 2 * 1024 * 1024) kill();
      });
      child.on("error", () => {
        clearTimeout(timer);
        reject(Error("BINARY_START_FAILED"));
      });
      child.on("exit", (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal, stdout });
      });
    });
  }
  const command = (args, input) =>
    exec(process.execPath, [cli, ...args], input);
  const action = async (args, input) => {
    const result = await command(args, input);
    assert.equal(result.code, 0, result.stdout);
    return JSON.parse(result.stdout);
  };
  try {
    const version = await exec(binary, ["--version"]);
    assert.equal(version.code, 0);
    assert.equal(version.stdout.trim(), "2.1.316 (Claude Code)");
    await action(
      [
        "auth",
        "add",
        "--profile",
        "fixture",
        "--project",
        "fixture",
        "--storage",
        "file",
        "--stdin",
        "--endpoint",
        origin + "/v1/traces",
        "--loopback-test",
        "--contract-manifest",
        manifest,
      ],
      JSON.stringify({
        api_key: "synthetic-api",
        secret_key: "synthetic-secret",
      }),
    );
    const enable = await action([
      "enable",
      "claude-code",
      "--surface",
      "cli",
      "--profile",
      "fixture",
    ]);
    assert.equal(enable.state, "waiting-for-activation");
    const before = await action(["doctor", "claude-code", "--surface", "cli"]);
    assert.equal(before.activation.activated, false);
    const agentArgs = [
      "--print",
      "--model",
      "claude-opus-5-5",
      "--output-format",
      "json",
      "Read fixture.txt then say fixture-ok. PRIVACY_CANARY_PROMPT",
    ];
    // Measure the binary's untraced cancellation semantics, then require the
    // launcher to preserve them. Do not presume a signal equals exit code 130.
    const baseline =
      scenario === "cancel"
        ? await exec(binary, agentArgs, "", true)
        : undefined;
    if (baseline) {
      assert.equal(cancellations, 1);
      assert.notEqual(baseline.signal, "SIGKILL", "baseline timed out");
      calls = 0;
    }
    const result = await exec(
      process.execPath,
      [cli, "run", "claude-code", "--surface", "cli", "--", ...agentArgs],
      "",
      scenario === "cancel",
    );
    if (baseline) {
      assert.equal(cancellations, 2);
      assert.equal(result.code, baseline.code);
      assert.equal(result.signal, baseline.signal);
    } else {
      assert.equal(result.code, scenario === "success" ? 0 : 1);
      assert.equal(result.signal, null);
      if (scenario === "success")
        assert.ok(result.stdout.includes("fixture-ok"));
    }
    // Explicit drain, with a bounded wait; loopback transport acceptance is not backend persistence.
    const until = Date.now() + 8000;
    let spans = [];
    do {
      await command(["flush", "--deadline", "1000"]);
      spans = uploads.flatMap((u) =>
        u.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
      );
      if (
        spans.some((s) =>
          s.attributes.some(
            (a) => a.key === "fi.span.kind" && a.value.stringValue === "AGENT",
          ),
        ) &&
        spans.filter((s) =>
          s.attributes.some(
            (a) => a.key === "fi.span.kind" && a.value.stringValue === "LLM",
          ),
        ).length ===
          (scenario === "success" ? 2 : scenario === "failure" ? 1 : 0)
      )
        break;
      await new Promise((r) => setTimeout(r, 100));
    } while (Date.now() < until);
    const attrs = (s) =>
      Object.fromEntries(
        s.attributes.map((a) => [
          a.key,
          a.value.stringValue ??
            a.value.doubleValue ??
            a.value.intValue ??
            a.value.boolValue,
        ]),
      );
    const requests = spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
    const tools = spans.filter((s) => attrs(s)["fi.span.kind"] === "TOOL");
    const completed = spans.filter((s) =>
      ["AGENT", "TOOL"].includes(attrs(s)["fi.span.kind"]),
    );
    for (const span of completed) {
      assert.equal(attrs(span)["fi.agent.outcome"], "unknown");
      assert.equal(span.status.code, 0);
      assert.notEqual(attrs(span)["fi.capture.partial"], true);
    }
    if (scenario === "success") {
      assert.equal(calls, 2);
      assert.equal(requests.length, 2);
      assert.equal(tools.length, 1);
      assert.equal(completed.length, 2);
      assert.ok(spans.some((s) => attrs(s)["fi.span.kind"] === "CHAIN"));
      assert.ok(
        requests.every((s) => attrs(s)["fi.agent.outcome"] === "success"),
      );
      assert.equal(
        requests.reduce(
          (n, s) => n + Number(attrs(s)["gen_ai.usage.input_tokens"]),
          0,
        ),
        40,
      );
      assert.equal(
        requests.reduce(
          (n, s) => n + Number(attrs(s)["gen_ai.usage.output_tokens"]),
          0,
        ),
        4,
      );
    } else if (scenario === "failure") {
      assert.ok(calls >= 1);
      assert.equal(
        requests.length,
        1,
        "HTTP retries are one logical native request",
      );
      assert.equal(completed.length, 1);
      assert.equal(tools.length, 0);
      assert.equal(attrs(requests[0])["fi.agent.outcome"], "error");
      assert.equal(requests[0].status.code, 2);
    } else {
      assert.equal(calls, 1);
      assert.equal(tools.length, 0);
      // This pinned SIGINT path completes the wrapper but omits the interrupted
      // request span. Neither the exit code (0) nor the wrapper proves success.
      assert.equal(completed.length, 1);
      assert.equal(requests.length, 0);
    }
    assert.ok(!JSON.stringify(uploads).includes("PRIVACY_CANARY"));
    assert.ok(!JSON.stringify(uploads).includes(home));
    assert.ok(spans.every((s) => attrs(s)["fi.capture.source"] === "native"));
    const ids = new Set(spans.map((s) => s.spanId));
    if (scenario !== "cancel")
      assert.ok(requests.every((s) => ids.has(s.parentSpanId)));
    const doctor = await action(["doctor", "claude-code", "--surface", "cli"]);
    assert.equal(doctor.activation.activated, spans.length > 0);
    assert.equal(doctor.persistence, "unknown");
    await action(["disable", "claude-code", "--surface", "cli"]);
    const disabled = await command([
      "run",
      "claude-code",
      "--surface",
      "cli",
      "--",
      "--print",
      "ignored",
    ]);
    assert.equal(disabled.code, 3);
    const after = await action(["doctor", "claude-code", "--surface", "cli"]);
    assert.equal(after.activation.activated, false);
    // Check owned capture database/WAL only, never original agent transcripts.
    for (const filename of fs.readdirSync(path.join(home, "s/spool"))) {
      const file = path.join(home, "s/spool", filename);
      if (fs.statSync(file).isFile())
        assert.ok(
          !fs.readFileSync(file).includes(Buffer.from("PRIVACY_CANARY")),
        );
    }
    return {
      binaryVersion: "2.1.316",
      scenario,
      childExit: result.code,
      childSignal: result.signal,
      ...(baseline
        ? {
            baselineExit: baseline.code,
            baselineSignal: baseline.signal,
            cancellations,
          }
        : {}),
      surface: "cli-print",
      modelRequests: calls,
      nativeSpans: spans.length,
      requestSpans: requests.length,
      toolSpans: tools.length,
      ...(scenario === "success" ? { inputTokens: 40, outputTokens: 4 } : {}),
      privacyBeforeSpool: true,
      doctorActivation: doctor.activation.activated,
      disableHeld: true,
      wrapperOutcomes: completed.map((s) => ({
        name: s.name,
        outcome: attrs(s)["fi.agent.outcome"],
        status: s.status.code,
      })),
      requestOutcomes: requests.map((s) => ({
        outcome: attrs(s)["fi.agent.outcome"],
        status: s.status.code,
      })),
      terminalCapture: completed.length ? "observed" : "unavailable",
      modelNetwork: "loopback-only-sandbox",
      backendPersistence: "NOT_TESTED",
      packageRoot,
      home,
    };
  } finally {
    await command(["runtime", "stop"]).catch(() => undefined);
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  }
}
module.exports = { run };
if (require.main === module) {
  const [binary, root, sandbox, packageRoot, scenario] = process.argv.slice(2);
  run({ binary, root, sandbox, packageRoot, scenario }).then(
    (result) => console.log(JSON.stringify(result)),
    (e) => {
      console.error(e.message);
      process.exitCode = 1;
    },
  );
}
