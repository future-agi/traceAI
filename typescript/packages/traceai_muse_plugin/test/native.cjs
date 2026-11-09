// Runs only in E2B, as a dedicated uid with non-loopback egress rejected.
// All homes, credentials, provider responses and project text are synthetic.
const { createServer } = require("node:http");
const { spawn } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
const { EVENTS } = require("../dist/index.js");
const out = "/work/evidence";
const cli = "/work/installed/node_modules/@traceai/agents/dist/cli.js";
const muse = "/usr/local/bin/muse";
let env,
  modelCalls = 0,
  toolCalls = 0;
const otlp = [],
  requests = [];
function run(bin, args, label, expected = [0]) {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, {
      env,
      cwd: "/work/evidence/project",
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (b) => (stdout += b));
    child.stderr.on("data", (b) => (stderr += b));
    const timer = setTimeout(() => child.kill("SIGKILL"), 90000);
    child.on("error", reject);
    child.on("close", async (code) => {
      clearTimeout(timer);
      await fs.writeFile(
        `${out}/${label}.json`,
        JSON.stringify({ code, stdout, stderr }, null, 2),
      );
      if (!expected.includes(code))
        reject(new Error(`${label} exited ${code}: ${stderr.slice(-800)}`));
      else resolve({ code, stdout, stderr });
    });
  });
}
const listen = (s) => new Promise((r) => s.listen(0, "127.0.0.1", r));
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [
      a.key,
      a.value.stringValue ?? a.value.intValue ?? a.value.boolValue,
    ]),
  );
const spans = () =>
  otlp.flatMap((x) =>
    (x.resourceSpans || []).flatMap((r) =>
      (r.scopeSpans || []).flatMap((s) => s.spans || []),
    ),
  );
async function main() {
  await fs.mkdir(`${out}/project`, { recursive: true });
  const home = await fs.mkdtemp("/tmp/fi-muse-native-");
  const config = `${home}/.config/muse`;
  await fs.mkdir(config, { recursive: true, mode: 0o700 });
  const capture = createServer((req, res) => {
    let body = "";
    req.on("data", (b) => (body += b));
    req.on("end", () => {
      otlp.push(JSON.parse(body));
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  const provider = createServer((req, res) => {
    let body = "";
    req.on("data", (b) => (body += b));
    req.on("end", () => {
      requests.push({
        method: req.method,
        path: req.url,
        body: body ? JSON.parse(body) : null,
      });
      if (req.method === "GET" && req.url.endsWith("/muse-code/models")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(
          JSON.stringify({
            object: "list",
            data: [
              {
                id: "fake-model",
                object: "model",
                metadata: {
                  "muse-code": {
                    release_date: "2026-01-01",
                    is_hidden: false,
                    limit: { context: 1000000, output: 1024 },
                  },
                },
              },
            ],
          }),
        );
        return;
      }
      if (!req.url.endsWith("/responses")) {
        res.writeHead(404);
        res.end("{}");
        return;
      }
      modelCalls++;
      const tool = body.includes("MUSE_TOOL_FIXTURE") && !toolCalls;
      if (tool) toolCalls++;
      const id = `resp_fixture_${modelCalls}`;
      const frame = (status) => ({
        id,
        object: "response",
        model: "fake-model",
        status,
        output: [],
      });
      const events = [
        {
          type: "response.created",
          sequence_number: 1,
          response: frame("in_progress"),
        },
        tool
          ? {
              type: "response.function_call_arguments.done",
              sequence_number: 2,
              output_index: 0,
              item_id: "fc_fixture",
              name: "bash",
              call_id: "call_fixture",
              arguments: JSON.stringify({
                command: "printf muse-fixture",
                description: "Print synthetic validation text",
              }),
            }
          : {
              type: "response.output_text.delta",
              sequence_number: 2,
              output_index: 0,
              item_id: `msg_${modelCalls}`,
              content_index: 0,
              delta: "Synthetic Muse fixture response.",
            },
        {
          type: "response.completed",
          sequence_number: 3,
          response: {
            ...frame("completed"),
            usage: { input_tokens: 11, output_tokens: 5, total_tokens: 16 },
          },
        },
      ];
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.end(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""));
    });
  });
  await listen(capture);
  await listen(provider);
  env = {
    MUSE_EXPERIMENTAL_PLUGINS: "1",
    HOME: home,
    PATH: process.env.PATH,
    LANG: "C.UTF-8",
    TERM: "dumb",
    FI_API_KEY: "synthetic-api",
    FI_SECRET_KEY: "synthetic-secret",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${capture.address().port}/tracer/v1/traces`,
  };
  try {
    await fs.writeFile(
      `${config}/settings.json`,
      JSON.stringify({
        schema_version: 1,
        endpoint_transport: {
          base_url: `http://127.0.0.1:${provider.address().port}`,
          auth: "bearer",
        },
      }),
      { mode: 0o600 },
    );
    await fs.writeFile(
      `${config}/auth.json`,
      JSON.stringify({
        schema_version: 1,
        providers: { meta: { api_key: "synthetic-meta-fixture" } },
      }),
      { mode: 0o600 },
    );
    await run(muse, ["--version"], "version");
    await run(muse, ["exec", "--help"], "exec-help");
    const setup = await run(
      process.execPath,
      [cli, "setup", "muse", "--project", "coding-agents", "--json"],
      "setup",
      [2],
    );
    assert.equal(JSON.parse(setup.stdout).code, "TRUST_REQUIRED");
    await run(
      muse,
      ["plugins", "inspect", "futureagi-tracing", "--json"],
      "pending",
    );
    await run(
      muse,
      ["exec", "--provider", "echo", "unapproved fixture"],
      "unapproved",
    );
    assert.equal(spans().length, 0, "unapproved hooks must not export");
    // Explicit approval is a test action in this isolated synthetic home, never part of setup.
    await run(
      muse,
      ["plugins", "approve", "futureagi-tracing", "--json"],
      "approved",
    );
    await run(
      process.execPath,
      [cli, "setup", "muse", "--project", "coding-agents", "--json"],
      "repeat-setup",
    );
    // Capture actual hook schemas in a separate test-only observer, not by modifying the shipped plugin.
    const probe = `${out}/probe`;
    await fs.mkdir(`${probe}/.muse-plugin`, { recursive: true });
    await fs.mkdir(`${probe}/hooks`, { recursive: true });
    for (const event of EVENTS)
      await fs.writeFile(
        `${probe}/hooks/${event.toLowerCase()}.cjs`,
        `let s='';process.stdin.on('data',b=>s+=b);process.stdin.on('end',()=>require('node:fs').appendFileSync('${out}/hooks.jsonl',s.trim()+'\\n'));`,
      );
    await fs.writeFile(
      `${probe}/.muse-plugin/plugin.json`,
      JSON.stringify({
        schemaVersion: 1,
        name: "fixture-probe",
        version: "0.1.0",
        description: "Synthetic sandbox hook recorder",
        compat: { source: "native", manifestDir: ".muse-plugin" },
        capabilities: {
          hooks: EVENTS.map((event) => ({
            id: event.toLowerCase(),
            event,
            command: [process.execPath, `hooks/${event.toLowerCase()}.cjs`],
            timeoutMs: 2000,
          })),
        },
      }),
    );
    await run(
      muse,
      ["plugins", "install", probe, "--scope", "user", "--json"],
      "probe-install",
    );
    await run(
      muse,
      ["plugins", "approve", "fixture-probe", "--json"],
      "probe-approve",
    );
    await run(
      muse,
      [
        "exec",
        "--provider",
        "echo",
        "--disable-reminders",
        "--trust-workspace",
        "--session-id",
        "aaaaaaaa-1111-4111-8111-111111111111",
        "lifecycle fixture",
      ],
      "echo",
    );
    const echoRoots = spans().filter((s) => !s.parentSpanId).length;
    await run(
      muse,
      [
        "export",
        "--session",
        "aaaaaaaa-1111-4111-8111-111111111111",
        "--out",
        `${out}/echo-transcript.json`,
      ],
      "echo-export",
      [0, 1, 2],
    );
    assert.equal(
      spans().filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      0,
    );
    const before = spans().length;
    await run(
      muse,
      [
        "exec",
        "--json",
        "--trust-workspace",
        "--disable-reminders",
        "--disable-web-tools",
        "--approval-judge",
        "off",
        "--max-model-steps",
        "4",
        "--session-id",
        "bbbbbbbb-2222-4222-8222-222222222222",
        "MUSE_TOOL_FIXTURE: print synthetic text then answer",
      ],
      "model",
      [0, 1],
    );
    await run(
      muse,
      [
        "exec",
        "--json",
        "--trust-workspace",
        "--disable-reminders",
        "--disable-web-tools",
        "--approval-judge",
        "off",
        "--max-model-steps",
        "4",
        "--session-id",
        "bbbbbbbb-2222-4222-8222-222222222222",
        "Second synthetic turn in the same conversation",
      ],
      "model-second",
    );
    await run(
      muse,
      [
        "export",
        "--session",
        "bbbbbbbb-2222-4222-8222-222222222222",
        "--out",
        `${out}/model-transcript.json`,
      ],
      "model-export",
      [0, 1, 2],
    );
    await run(
      process.execPath,
      [cli, "status", "muse", "--json", "--check"],
      "status",
      [0, 2],
    );
    const realSpanCount = spans().length;
    // Native hook-test is separate contract evidence, never counted as real session tracing.
    for (const [event, extra] of [
      ["UserPromptSubmit", { prompt: "native hook fixture" }],
      ["Stop", { last_assistant_message: "fixture finished" }],
    ]) {
      const file = `${out}/fixture-${event}.json`;
      await fs.writeFile(
        file,
        JSON.stringify({
          event,
          stdin: {
            hook_event_name: event,
            session_id: "fixture-session",
            turn_id: "fixture-turn",
            ...extra,
          },
        }),
      );
      await run(
        muse,
        [
          "plugins",
          "hook",
          "test",
          `futureagi-tracing:${event.toLowerCase()}`,
          "--fixture",
          file,
          "--json",
        ],
        `hook-test-${event}`,
      );
    }
    await run(process.execPath, [cli, "disable", "muse", "--json"], "disabled");
    const disabledCount = spans().length;
    await run(
      muse,
      ["exec", "--provider", "echo", "disabled fixture"],
      "disabled-echo",
    );
    assert.equal(spans().length, disabledCount);
    await run(
      process.execPath,
      [cli, "uninstall", "muse", "--json"],
      "uninstall",
    );
    await run(muse, ["plugins", "list", "--json"], "uninstalled-list");
    await fs.writeFile(
      `${out}/summary.json`,
      JSON.stringify(
        {
          home,
          echoRoots,
          realSpanCount,
          hookTestSpans: spans().length - realSpanCount,
          modelCalls,
          toolCalls,
          echoSpanCount: before,
          totalSpans: spans().length,
          spans: spans().map((s) => ({
            name: s.name,
            parent: !!s.parentSpanId,
            attributes: attrs(s),
          })),
        },
        null,
        2,
      ),
    );
    assert.equal(echoRoots, 1, "echo must produce exactly one real turn");
    assert.equal(
      modelCalls,
      3,
      "two requests plus one continuation request must reach the local model",
    );
    const real = spans().slice(0, realSpanCount);
    const roots = real.filter((s) => !s.parentSpanId);
    assert.equal(roots.length, 3, "one echo turn and two model turns");
    const models = real.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
    assert.equal(models.length, 3);
    assert.equal(
      models.reduce(
        (sum, span) => sum + Number(attrs(span)["gen_ai.usage.input_tokens"]),
        0,
      ),
      33,
    );
    assert.equal(
      models.reduce(
        (sum, span) => sum + Number(attrs(span)["gen_ai.usage.output_tokens"]),
        0,
      ),
      15,
    );
    assert.equal(
      real.filter((s) => attrs(s)["fi.span.kind"] === "TOOL").length,
      1,
    );
    assert.equal(attrs(roots[1])["session.id"], attrs(roots[2])["session.id"]);
    for (const span of real.filter((s) => s.parentSpanId))
      assert(
        roots.some(
          (root) =>
            root.spanId === span.parentSpanId && root.traceId === span.traceId,
        ),
      );
    assert.equal(
      spans().length - realSpanCount,
      1,
      "native hook-test exports exactly its separate fixture turn",
    );
  } finally {
    await fs.writeFile(`${out}/otlp.json`, JSON.stringify(otlp, null, 2));
    await fs.writeFile(
      `${out}/provider-requests.json`,
      JSON.stringify(requests, null, 2),
    );
    capture.closeAllConnections();
    provider.closeAllConnections();
    await Promise.all([
      new Promise((r) => capture.close(r)),
      new Promise((r) => provider.close(r)),
    ]);
  }
}
main().catch(async (e) => {
  await fs.writeFile(`${out}/failure.txt`, e.stack);
  console.error(e.message);
  process.exitCode = 1;
});
