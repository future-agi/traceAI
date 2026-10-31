#!/usr/bin/env node
"use strict";
// Explicit, synthetic-only qualification. Never use a real agent home or key.
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const {
  importCodex,
  CODEX_SCHEMA,
} = require("../dist/src/adapters/codex/codec.js");
const { decodeTraces } = require("../dist/src/export/otlp.js");
async function qualify({ root, binary, sandbox }) {
  assert.equal(process.platform, "darwin");
  for (const p of [root, binary, sandbox]) assert.ok(path.isAbsolute(p));
  assert.ok(!fs.existsSync(root), "qualification root must be new");
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const home = path.join(root, "h"),
    agent = path.join(root, "a"),
    state = path.join(root, "s");
  for (const p of [home, agent, state]) fs.mkdirSync(p, { mode: 0o700 });
  const env = {
    PATH: path.dirname(process.execPath) + ":/usr/bin:/bin",
    HOME: home,
    CODEX_HOME: agent,
    CLAUDE_CONFIG_DIR: path.join(root, "unused"),
    TMPDIR: root,
    FI_AGENT_CONFIG_HOME: path.join(root, "c"),
    FI_AGENT_STATE_HOME: state,
    NO_PROXY: "127.0.0.1,localhost",
  };
  const cli = path.resolve(__dirname, "../bin/fi-agent.cjs");
  async function command(exe, args, input, code = 0) {
    return new Promise((resolve, reject) => {
      const child = spawn(exe, args, {
        cwd: home,
        env,
        stdio: ["pipe", "pipe", "pipe"],
        detached: true,
      });
      let out = "",
        err = "",
        bytes = 0;
      const kill = () => {
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch {
          child.kill("SIGKILL");
        }
      };
      const timer = setTimeout(kill, 60000);
      child.stdout.on("data", (b) => {
        bytes += b.length;
        if (bytes > 2097152) kill();
        else out += b;
      });
      child.stderr.on("data", (b) => {
        bytes += b.length;
        if (bytes > 2097152) kill();
        else err += b;
      });
      child.on("error", reject);
      child.on("exit", (c) => {
        clearTimeout(timer);
        if (c !== code)
          reject(
            Error(
              "command failed " +
                JSON.stringify({
                  exe: path.basename(exe),
                  args: args.slice(0, 3),
                  code: c,
                  stdout: out.slice(0, 400),
                  stderr: err.slice(0, 800),
                }),
            ),
          );
        else resolve(out);
      });
      child.stdin.end(input);
    });
  }
  const codex = (args) =>
    command("/usr/bin/sandbox-exec", ["-f", sandbox, binary, ...args]);
  const fi = (args, input, code = 0) =>
    command(process.execPath, [cli, ...args], input, code);
  let calls = 0;
  const uploads = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 2097152) {
        res.writeHead(413).end();
        return;
      }
      chunks.push(chunk);
    }
    if (req.url === "/traces") {
      uploads.push(decodeTraces(Buffer.concat(chunks)));
      res.writeHead(200, { "content-type": "application/x-protobuf" }).end();
      return;
    }
    if (!req.url.includes("/responses")) {
      res.writeHead(404).end();
      return;
    }
    const body = JSON.parse(Buffer.concat(chunks));
    calls++;
    const tool = calls === 1;
    const item = tool
      ? {
          id: "fc_fixture",
          type: "function_call",
          name: "update_plan",
          call_id: "call_fixture",
          arguments: JSON.stringify({
            plan: [{ step: "Synthetic observer fixture", status: "completed" }],
          }),
          status: "completed",
        }
      : {
          id: "msg_fixture_" + calls,
          type: "message",
          role: "assistant",
          status: "completed",
          content: [
            { type: "output_text", text: "fixture-ok", annotations: [] },
          ],
        };
    const response = {
      id: "resp_fixture_" + calls,
      object: "response",
      status: "completed",
      created_at: 1,
      model: body.model,
      output: [item],
      usage: {
        input_tokens: 10,
        output_tokens: 2,
        total_tokens: 12,
        input_tokens_details: { cached_tokens: 3 },
        output_tokens_details: { reasoning_tokens: 1 },
      },
    };
    res.writeHead(200, { "content-type": "text/event-stream" });
    for (const [sequence_number, event] of [
      {
        type: "response.created",
        response: { ...response, status: "in_progress", output: [] },
      },
      { type: "response.output_item.added", output_index: 0, item },
      ...(tool
        ? []
        : [
            {
              type: "response.output_text.delta",
              item_id: item.id,
              output_index: 0,
              content_index: 0,
              delta: "fixture-ok",
            },
          ]),
      { type: "response.output_item.done", output_index: 0, item },
      { type: "response.completed", response },
    ].entries())
      res.write(
        "event: " +
          event.type +
          "\ndata: " +
          JSON.stringify({ ...event, sequence_number }) +
          "\n\n",
      );
    res.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const origin = "http://127.0.0.1:" + server.address().port;
  const manifest = path.join(root, "contract.json");
  fs.writeFileSync(
    manifest,
    JSON.stringify({
      contract: "immutable-agent-v1",
      endpoint: origin + "/traces",
      project: "fixture",
      immutableAdmission: true,
      uniqueReads: true,
      usagePlans: true,
    }),
    { mode: 0o600 },
  );
  const configFile = path.join(agent, "config.toml");
  fs.writeFileSync(
    configFile,
    `model="fixture-model"\nmodel_provider="fixture"\n[features]\nhooks=true\n[model_providers.fixture]\nname="Fixture"\nbase_url="${origin}/v1"\nwire_api="responses"\nrequires_openai_auth=false\n`,
    { mode: 0o600 },
  );
  let runtime = false;
  try {
    const version = (await codex(["--version"])).trim();
    await fi(
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
        origin + "/traces",
        "--loopback-test",
        "--contract-manifest",
        manifest,
      ],
      '{"api_key":"fixture-only","secret_key":"fixture-only"}',
    );
    const enabled = JSON.parse(
      await fi([
        "enable",
        "codex",
        "--surface",
        "cli",
        "--profile",
        "fixture",
        "--project",
        "fixture",
        "--capture",
        "structure",
        "--transcript",
        "structure",
      ]),
    );
    runtime = true;
    assert.equal(enabled.state, "waiting-for-activation");
    const registration = JSON.parse(
      fs.readFileSync(path.join(state, "codex/registration.json")),
    );
    await codex(["plugin", "marketplace", "add", registration.bundle]);
    await codex(["plugin", "add", "traceai-codex@traceai-observers"]);
    const before = JSON.parse(
      await fi(["doctor", "codex", "--surface", "cli"]),
    );
    assert.equal(before.activation.activated, false);
    // Obtain upstream-computed trust hashes, exclusively in this new synthetic
    // HOME. Production enable never grants trust or changes feature settings.
    const app = spawn(
      "/usr/bin/sandbox-exec",
      ["-f", sandbox, binary, "app-server"],
      { cwd: home, env, stdio: ["pipe", "pipe", "pipe"] },
    );
    const pending = new Map();
    let serial = 1,
      buffer = "";
    app.stderr.resume();
    app.stdout.on("data", (chunk) => {
      buffer += chunk;
      for (;;) {
        const n = buffer.indexOf("\n");
        if (n < 0) break;
        const l = buffer.slice(0, n);
        buffer = buffer.slice(n + 1);
        try {
          const m = JSON.parse(l);
          if (pending.has(m.id)) {
            pending.get(m.id)(m);
            pending.delete(m.id);
          }
        } catch {}
      }
    });
    async function rpc(method, params) {
      const id = serial++;
      const p = new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(Error("APP_SERVER_TIMEOUT")),
          15000,
        );
        pending.set(id, (m) => {
          clearTimeout(timer);
          m.error
            ? reject(Error("APP_SERVER_ERROR " + JSON.stringify(m.error)))
            : resolve(m.result);
        });
      });
      app.stdin.write(JSON.stringify({ id, method, params }) + "\n");
      return p;
    }
    let hooks;
    try {
      await rpc("initialize", {
        clientInfo: { name: "traceai-contract", version: "0.1.0" },
        capabilities: { experimentalApi: true },
      });
      app.stdin.write('{"method":"initialized"}\n');
      hooks = (await rpc("hooks/list", { cwds: [home] })).data
        .flatMap((e) => e.hooks)
        .filter((h) => h.pluginId === "traceai-codex@traceai-observers");
      assert.equal(hooks.length, 10);
      for (const h of hooks) {
        const [declaration, content] = h.currentHash.split("/");
        fs.appendFileSync(
          configFile,
          `\n[hooks.state.${JSON.stringify(h.key)}]\ntrusted_hash=${JSON.stringify(declaration)}\ntrusted_content_hash=${JSON.stringify(content)}\n`,
        );
      }
    } finally {
      app.kill("SIGTERM");
      await once(app, "exit");
    }
    const output = await codex([
      "exec",
      "--skip-git-repo-check",
      "--sandbox",
      "read-only",
      "--json",
      "Use update_plan for one completed synthetic step, then say fixture-ok.",
    ]);
    assert.ok(output.includes("fixture-ok"));
    // Wait for the off-path two-second reconciliation and export, bounded.
    const deadline = Date.now() + 15000;
    let status;
    do {
      status = JSON.parse(await fi(["status"]));
      if (status.agents[0]?.observer?.admitted >= 4 && uploads.length >= 4)
        break;
      await new Promise((r) => setTimeout(r, 250));
    } while (Date.now() < deadline);
    const doctor = JSON.parse(
      await fi(["doctor", "codex", "--surface", "cli"]),
    );
    assert.equal(doctor.activation.activated, true);
    const files = [];
    function walk(p) {
      for (const e of fs.readdirSync(p, { withFileTypes: true })) {
        const f = path.join(p, e.name);
        if (e.isDirectory()) walk(f);
        else if (e.name.endsWith(".jsonl")) files.push(f);
      }
    }
    walk(path.join(agent, "sessions"));
    assert.equal(files.length, 1);
    const facts = importCodex(fs.readFileSync(files[0]), {
      identityKey: Buffer.alloc(32, 1),
      policyRevision: "1",
      sourceSchema: CODEX_SCHEMA,
    });
    const requests = facts.filter((f) => f.unit === "request"),
      tools = facts.filter((f) => f.unit === "tool");
    assert.equal(requests.length, calls);
    assert.equal(calls, 2);
    assert.equal(tools.length, 1);
    assert.equal(
      requests.reduce((s, f) => s + f.usage.total, 0),
      24,
    );
    assert.ok(
      requests.every((f) => f.timing === "unknown" && f.startNs === f.endNs),
    );
    assert.equal(status.agents[0].observer.admitted, facts.length);
    assert.equal(
      uploads.flatMap((u) =>
        u.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
      ).length,
      facts.length,
    );
    const plan = JSON.parse(
      await fi([
        "import",
        "codex",
        "--surface",
        "cli",
        "--path",
        files[0],
        "--dry-run",
      ]),
    );
    const replay = JSON.parse(
      await fi([
        "import",
        "codex",
        "--surface",
        "cli",
        "--path",
        files[0],
        "--commit",
        plan.plan_id,
      ]),
    );
    assert.equal(replay.counts.pending, 0);
    await fi(["disable", "codex", "--surface", "cli"]);
    const disabled = JSON.parse(
      await fi(["doctor", "codex", "--surface", "cli"]),
    );
    assert.equal(disabled.activation.activated, false);
    const result = {
      version,
      model_calls: calls,
      request_spans: requests.length,
      tool_spans: tools.length,
      turn_spans: facts.filter((f) => f.unit === "turn").length,
      total_tokens: 24,
      hook_events_registered: hooks.length,
      live: status.agents[0].observer,
      import_replay: replay.counts,
      activation: doctor.activation,
      disabled: true,
      outbound: "sandbox-denied-except-localhost",
      remote_persistence: "not-tested-synthetic-loopback-only",
    };
    fs.writeFileSync(
      path.join(root, "result.json"),
      JSON.stringify(result, null, 2),
    );
    return result;
  } finally {
    if (runtime) await fi(["runtime", "stop"]).catch(() => {});
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  }
}
if (require.main === module) {
  qualify({
    root: process.argv[2],
    binary: process.argv[3],
    sandbox: process.argv[4],
  }).then(
    (x) => console.log(JSON.stringify(x)),
    (e) => {
      console.error(e);
      process.exitCode = 1;
    },
  );
}
module.exports = { qualify };
