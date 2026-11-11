// Claude Code transcript images through the provider-owned attachment core (G03).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const core = require("@traceai/agent-core");
const adapter = require("../dist/index.js");
const { fake } = require("../../fi-core/test/attachments-fixtures.cjs");

const session = "8a472dc2-ce2f-48ef-ade3-07ec9791f6d3",
  prompt = "0bb7337e-58a1-48f0-806d-492fca3487db";
// Two distinct synthetic PNGs so prompt and tool-result bytes are distinguishable.
const PROMPT_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==";
const TOOL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2NgYPj/HwADAgH/o1FYqQAAAABJRU5ErkJggg==";
const image = (data, extra = {}) => ({
  type: "image",
  source: { type: "base64", media_type: "image/png", data },
  ...extra,
});

async function transcriptRows({ toolError = false } = {}) {
  const rows = (
    await fs.readFile(
      path.join(__dirname, "fixtures/hooks-transcript-2.1.318.jsonl"),
      "utf8",
    )
  )
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  rows[0].message.content = [
    { type: "text", text: "SAMPLE_USER_PROMPT: describe this screenshot." },
    image(PROMPT_PNG),
    // Retrieval sources are not inline bytes and must never be fetched.
    { type: "image", source: { type: "url", url: "https://example.invalid/signed-secret" } },
  ];
  rows[2].message.content[0].content = [
    { type: "text", text: "SAMPLE_TOOL_OUTPUT" },
    image(TOOL_PNG),
  ];
  if (toolError) rows[2].message.content[0].is_error = true;
  return rows;
}

async function fixture(t, overrides = {}, rowOptions = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-claude-media-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const received = [];
  const server = http.createServer(async (req, res) => {
    received.push(JSON.parse(Buffer.concat(await Array.fromAsync(req))));
    res.writeHead(200, { "content-type": "application/json" }).end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const env = {
    HOME: home,
    CLAUDE_CONFIG_DIR: home + "/.claude",
    XDG_DATA_HOME: home + "/.local/share",
    FI_API_KEY: "dummy-fi-api",
    FI_SECRET_KEY: "dummy-fi-secret",
    FI_PROJECT_NAME: "claude-test",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces`,
    FI_AGENT_ENABLED: "true",
    FI_ATTACHMENTS_ENABLED: "true",
    ...overrides,
  };
  const original = { ...process.env };
  for (const [k, v] of Object.entries(env))
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  t.after(() => {
    for (const k of Object.keys(process.env))
      if (!(k in original)) delete process.env[k];
    Object.assign(process.env, original);
  });
  const config = await core.resolveConfig({ agent: "claude", home, env });
  const directory = home + "/.claude/projects/sample";
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const transcript = directory + "/" + session + ".jsonl";
  await fs.writeFile(
    transcript,
    (await transcriptRows(rowOptions)).map((r) => JSON.stringify(r)).join("\n") + "\n",
    { mode: 0o600 },
  );
  const state = () => ({
    schemaVersion: 1,
    offset: 0,
    promptId: prompt,
    start: 1793958308016,
    tools: { toolu_probe: { start: 1793958308102, end: 1793958308204 } },
    subagents: [],
    compactions: [],
    completed: [],
  });
  const broker = fake();
  const uploads = [];
  const transport = {
    ...broker.transport,
    async initialize(s, request, signal) {
      uploads.push({ request });
      return broker.transport.initialize(s, request, signal);
    },
    async upload(s, upload, data) {
      // Two transfer workers run concurrently; match the body to its own upload.
      uploads.find((u) => u.request.attachment_id === upload.attachment_id).bytes =
        Buffer.from(data instanceof Uint8Array ? data : new Uint8Array(await data.arrayBuffer()));
    },
  };
  return { home, config, transcript, state, received, broker, uploads, transport };
}
const spans = (b) =>
  b.flatMap((x) => (x.resourceSpans || []).flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)));
const attrs = (s) =>
  Object.fromEntries(s.attributes.map((a) => [a.key, a.value.stringValue ?? a.value.intValue ?? a.value.boolValue]));

test("prompt and tool-result images upload byte-exact on their own spans without a prior ready()", async (t) => {
  const f = await fixture(t);
  // No ready() here: the export itself must wait for bootstrap or every image is skipped.
  await adapter.exportTranscriptTurn(f.config, f.state(), f.transcript, session, "success", {
    attachments: { transport: f.transport },
  });
  assert.equal(f.uploads.length, 2, "one prompt image and one tool-result image");
  const s = spans(f.received);
  const root = s.find((x) => !x.parentSpanId);
  const tool = s.find((x) => attrs(x)["fi.span.kind"] === "TOOL");
  const forRoot = f.uploads.find((u) => u.request.association.parent.span_id === root.spanId);
  const forTool = f.uploads.find((u) => u.request.association.parent.span_id === tool.spanId);
  assert.ok(forRoot && forTool);
  assert.deepEqual(forRoot.bytes, Buffer.from(PROMPT_PNG, "base64"));
  assert.deepEqual(forTool.bytes, Buffer.from(TOOL_PNG, "base64"));
  assert.equal(forRoot.request.direction, "input");
  assert.equal(forRoot.request.purpose, "model_input");
  assert.equal(forTool.request.direction, "output");
  assert.equal(forTool.request.purpose, "tool_result");
  assert.equal(forTool.request.tool_call_id, "toolu_probe");
  assert.equal(forRoot.request.association.parent.trace_id.replaceAll("-", ""), root.traceId);
  assert.ok(attrs(root)["fi.attachment.refs"].includes(forRoot.request.occurrence_id));
  assert.ok(attrs(tool)["fi.attachment.refs"].includes(forTool.request.occurrence_id));
  const otlp = JSON.stringify(f.received);
  for (const secret of [PROMPT_PNG, TOOL_PNG, "signed-secret", "synthetic-write-capability"])
    assert.equal(otlp.includes(secret), false, `${secret} absent from OTLP`);
});

test("a deferred non-final export leaves prompt media to the final root export", async (t) => {
  const f = await fixture(t);
  await adapter.exportTranscriptTurn(f.config, f.state(), f.transcript, session, "success", {
    final: false,
    attachments: { transport: f.transport },
  });
  assert.equal(f.uploads.filter((u) => u.request.purpose === "model_input").length, 0);
});

for (const [name, env, expected] of [
  ["disabled by default", { FI_ATTACHMENTS_ENABLED: undefined }, 0],
  ["explicitly disabled", { FI_ATTACHMENTS_ENABLED: "false" }, 0],
  ["content capture off", { FI_AGENT_CAPTURE_CONTENT: "false" }, 0],
  ["hide inputs", { FI_HIDE_INPUTS: "true" }, 0],
  ["hide input images", { FI_HIDE_INPUT_IMAGES: "true" }, 0],
  ["PII redaction without sanitizer", { FI_PII_REDACTION: "true" }, 0],
  ["hide outputs keeps the model-bound prompt image only", { FI_HIDE_OUTPUTS: "true" }, 1],
])
  test(`${name}: no binary egress beyond what policy allows`, async (t) => {
    const f = await fixture(t, env);
    await adapter.exportTranscriptTurn(f.config, f.state(), f.transcript, session, "success", {
      attachments: { transport: f.transport },
    });
    assert.equal(f.uploads.length, expected);
    if (expected === 0)
      assert.deepEqual(
        f.broker.calls.filter((c) => c !== "bootstrap"),
        [],
        "no initialize/upload/complete",
      );
    const otlp = JSON.stringify(f.received);
    assert.equal(otlp.includes(PROMPT_PNG) || otlp.includes(TOOL_PNG), false);
  });

test("hideErrorDetails denies an error tool-result image but keeps the prompt image", async (t) => {
  const f = await fixture(t, { FI_HIDE_ERROR_DETAILS: "true" }, { toolError: true });
  await adapter.exportTranscriptTurn(f.config, f.state(), f.transcript, session, "success", {
    attachments: { transport: f.transport },
  });
  assert.deepEqual(f.uploads.map((u) => u.request.purpose), ["model_input"]);
});

test("a symlinked transcript produces no read and no attachment egress", async (t) => {
  const f = await fixture(t);
  const link = path.join(path.dirname(f.transcript), "link.jsonl");
  await fs.symlink(f.transcript, link);
  await assert.rejects(
    adapter.exportTranscriptTurn(f.config, f.state(), link, session, "success", {
      attachments: { transport: f.transport },
    }),
  );
  assert.equal(f.uploads.length, 0);
  assert.deepEqual(f.broker.calls, []);
});

test("an unsupported attachment scope fails closed without blocking span export", async (t) => {
  const f = await fixture(t);
  const { AttachmentTransportError } = require("@traceai/fi-core/attachments");
  await adapter.exportTranscriptTurn(f.config, f.state(), f.transcript, session, "success", {
    attachments: {
      transport: {
        ...f.transport,
        async bootstrap() {
          throw new AttachmentTransportError("credential_scope_unsupported");
        },
      },
    },
  });
  assert.equal(f.uploads.length, 0);
  assert.equal(spans(f.received).length, 4, "spans still export");
});
