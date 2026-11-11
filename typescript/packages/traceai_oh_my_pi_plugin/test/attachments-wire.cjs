// Oh My Pi plugin over the real attachment HTTPS transport and OTLP, with the plugin's
// own provider-owned runtime. Cloud/CI only: the runner supplies a loopback certificate
// through NODE_EXTRA_CA_CERTS (read at process start) and its key as ATTACHMENT_TEST_KEY.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const https = require("node:https");
const { randomUUID } = require("node:crypto");
const plugin = require("../dist/index.js").default;
const { fake } = require("../../fi-core/test/attachments-fixtures.cjs");

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==";
const TOOL_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2NgYPj/HwADAgH/o1FYqQAAAABJRU5ErkJggg==";
const attrs = (s) => Object.fromEntries((s.attributes ?? []).map((a) => [a.key, Object.values(a.value)[0]]));
const until = async (check, ms = 5000) => {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error("timeout");
    await new Promise((r) => setTimeout(r, 20));
  }
};

async function main() {
  const broker = fake();
  const sessions = [], uploads = [], bytes = [], batches = [], completed = [];
  const active = new Map();
  let scopeDenied = false;
  const server = https.createServer({
    key: await fs.readFile(process.env.ATTACHMENT_TEST_KEY),
    cert: await fs.readFile(process.env.NODE_EXTRA_CA_CERTS),
  }, async (req, res) => {
    try {
      const chunks = []; for await (const c of req) chunks.push(c);
      const data = Buffer.concat(chunks);
      res.setHeader("content-type", "application/json");
      if (req.url === "/tracer/v1/attachment-sessions") {
        sessions.push(1);
        if (scopeDenied) { res.writeHead(403); res.end("{}"); return; }
        assert.equal(req.headers["x-api-key"], "synthetic-api");
        res.end(JSON.stringify(await broker.transport.bootstrap(JSON.parse(data)))); return;
      }
      if (req.url.endsWith("/attachments/uploads")) {
        const body = JSON.parse(data); uploads.push(body);
        const upload = { ...await broker.transport.initialize(null, body), upload_id: randomUUID() };
        active.set(upload.upload_id, upload);
        res.writeHead(201); res.end(JSON.stringify(upload)); return;
      }
      if (req.url.endsWith("/content") || req.url.endsWith("/complete")) {
        const id = req.url.split("/").at(-2), upload = active.get(id); assert.ok(upload);
        // Content/complete use only the narrow write capability, never the FI keys.
        assert.equal(req.headers.authorization, `Bearer ${upload.write_capability}`);
        assert.equal(req.headers["x-api-key"], undefined);
        if (req.url.endsWith("/content")) { bytes.push(data); res.end(JSON.stringify({ upload_id: id, state: "pending_upload" })); }
        else { completed.push(id); res.writeHead(202); res.end(JSON.stringify({ upload_id: id, state: "validating" })); }
        return;
      }
      if (req.url === "/tracer/v1/traces") { batches.push(JSON.parse(data)); res.end("{}"); return; }
      res.writeHead(404); res.end("{}");
    } catch { res.writeHead(500); res.end("{}"); }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const origin = `https://127.0.0.1:${server.address().port}`;
  const original = { ...process.env };
  const cases = [
    ["enabled", {}, 2], ["disabled-by-default", { FI_ATTACHMENTS_ENABLED: undefined }, 0],
    ["hide-inputs", { FI_HIDE_INPUTS: "true" }, 0], ["hide-outputs", { FI_HIDE_OUTPUTS: "true" }, 1],
    ["hide-input-images", { FI_HIDE_INPUT_IMAGES: "true" }, 0], ["pii", { FI_PII_REDACTION: "true" }, 0],
    ["content-off", { FI_AGENT_CAPTURE_CONTENT: "false" }, 0], ["scope-denied", {}, 0],
  ];
  try {
    for (const [name, flags, expected] of cases) {
      const home = await fs.mkdtemp(path.join(os.tmpdir(), "omp-media-"));
      for (const k of Object.keys(process.env)) if (!(k in original)) delete process.env[k];
      Object.assign(process.env, original, {
        HOME: home, FI_AGENT_ENABLED: "true", FI_AGENT_CAPTURE_CONTENT: "true",
        PI_CODING_AGENT_DIR: path.join(home, "agent"), FI_AGENT_CONFIG_DIR: path.join(home, "fi"),
        XDG_STATE_HOME: path.join(home, "state"), FI_API_KEY: "synthetic-api", FI_SECRET_KEY: "synthetic-secret",
        FI_PROJECT_NAME: "media", FI_OTLP_ENDPOINT: origin + "/tracer/v1/traces",
        FI_ATTACHMENTS_ENDPOINT: origin, FI_ATTACHMENTS_ENABLED: "true",
      });
      for (const [k, v] of Object.entries(flags)) if (v === undefined) delete process.env[k]; else process.env[k] = v;
      scopeDenied = name === "scope-denied";
      const handlers = {};
      plugin({ on(n, f) { handlers[n] = f; }, events: { on: () => () => undefined } });
      const ctx = { cwd: home, agent: { kind: "main", id: "Main" }, sessionManager: { getSessionId: () => "conversation-" + name },
        model: { id: "test", provider: "mock" } };
      const emit = async (type, e = {}) => handlers[type]?.({ type, ...e }, ctx);
      const start = { uploads: uploads.length, batches: batches.length, sessions: sessions.length, bytes: bytes.length };
      await emit("session_start");
      // Bootstrap is not awaited on the inference path; settle it explicitly in the harness.
      if (!["disabled-by-default", "content-off"].includes(name)) await until(() => sessions.length > start.sessions);
      await new Promise((r) => setTimeout(r, 200));
      await emit("before_agent_start", { reason: "prompt", prompt: "describe" });
      await emit("agent_start");
      await emit("message_end", { message: { role: "user", content: [{ type: "text", text: "describe" }, { type: "image", mimeType: "image/png", data: PNG }] } });
      await emit("tool_execution_start", { toolCallId: "image_call", toolName: "read", args: {} });
      await emit("tool_execution_end", { toolCallId: "image_call", toolName: "read", isError: false,
        result: { content: [{ type: "image", mimeType: "image/png", data: TOOL_PNG }] } });
      const t = Date.now();
      await emit("message_end", { message: { role: "assistant", timestamp: t, completedAt: t + 5, model: "test", provider: "mock",
        content: [{ type: "text", text: "Done" }], stopReason: "stop", usage: { input: 1, output: 1 } } });
      await emit("agent_end");
      await emit("session_shutdown");
      const caseUploads = uploads.slice(start.uploads);
      assert.equal(caseUploads.length, expected, name + ": attachment admission");
      const spans = batches.slice(start.batches).flatMap((b) => b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)));
      assert.ok(spans.length >= 2, name + ": spans still export");
      const otlp = JSON.stringify(spans);
      assert.equal(otlp.includes(PNG) || otlp.includes(TOOL_PNG), false, name + ": OTLP payload omission");
      assert.equal(otlp.includes("synthetic-write-capability"), false, name + ": capability absent from OTLP");
      if (name === "enabled") {
        const root = spans.find((s) => !s.parentSpanId);
        const tool = spans.find((s) => attrs(s)["tool.call_id"] === "image_call" || attrs(s)["gen_ai.tool.call.id"] === "image_call");
        assert.ok(root && tool, "root and tool spans");
        const prompt = caseUploads.find((u) => u.purpose === "model_input");
        const result = caseUploads.find((u) => u.purpose === "tool_result");
        assert.equal(prompt.association.parent.span_id, root.spanId);
        assert.equal(result.association.parent.span_id, tool.spanId);
        assert.equal(result.tool_call_id, "image_call");
        const caseBytes = bytes.slice(start.bytes).map((b) => b.toString("base64")).sort();
        assert.deepEqual(caseBytes, [PNG, TOOL_PNG].sort(), "byte-exact uploads");
        assert.ok(String(attrs(root)["fi.attachment.refs"]).includes(prompt.occurrence_id));
        assert.ok(String(attrs(tool)["fi.attachment.refs"]).includes(result.occurrence_id));
      }
      if (name === "hide-outputs") assert.equal(caseUploads[0].purpose, "model_input");
      console.log(JSON.stringify({ host: "oh-my-pi", harness: "plugin-api", scenario: name, uploads: caseUploads.length, status: "PASS" }));
      await fs.rm(home, { recursive: true, force: true });
    }
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in original)) delete process.env[k];
    Object.assign(process.env, original);
    server.closeAllConnections(); await new Promise((r) => server.close(r));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
