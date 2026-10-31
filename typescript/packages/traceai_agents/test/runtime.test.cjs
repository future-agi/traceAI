const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { saveConfig } = require("../dist/src/config.js");
const {
  FileSecretStore,
  newCredentials,
} = require("../dist/src/security/credentials.js");
const { observe } = require("@traceai/agent-core/client");
const {
  decodeTraces,
  encodeResponse,
  encodeFacts,
} = require("../dist/src/export/otlp.js");
const reference = require("../contract/reference.cjs");
const base = path.resolve(__dirname, "../../../../../runtime-tests");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
test("golden contract normalizes replay to the same four spans and exact inclusive totals", () => {
  const fixture = require("../../../../integrations/coding-agents/fixtures/normalized/reference-v1.json");
  const context = {
    identityKey: Buffer.alloc(32, 1),
    policyRevision: "1",
    sourceSchema: "fixture-v1",
  };
  const actual = fixture.raw.flatMap((raw) =>
    reference.normalize(raw, context),
  );
  assert.deepEqual(actual, fixture.canonical);
  assert.ok(!JSON.stringify(actual).includes("PRIVACY_CANARY"));
  const binding = {
    id: "00000000-0000-4000-8000-000000000001",
    endpoint: "https://api.futureagi.com/tracer/v1/traces",
    approvedOrigin: "https://api.futureagi.com",
    project: "fixture",
  };
  assert.deepEqual(
    decodeTraces(encodeFacts(actual, binding, "0.1.0")),
    fixture.otlp,
  );
  assert.equal(
    actual.reduce((s, f) => s + (f.usage?.input ?? 0), 0),
    200,
  );
  assert.equal(
    actual.reduce((s, f) => s + (f.usage?.output ?? 0), 0),
    40,
  );
  for (const f of actual.slice(1))
    assert.equal(f.parentSpanId, actual[0].spanId);
});
test("real storage child, local ACK, loopback export, replay and disable hold work together", async (t) => {
  const root = fs.mkdtempSync(path.join(base, "fixture-"));
  const previous = process.cwd();
  process.chdir(root);
  const p = {
    config: path.join(root, "config"),
    state: path.join(root, "state"),
    credentials: path.join(root, "credentials"),
    socket: "runtime.sock",
    configFile: path.join(root, "config/config.json"),
  };
  let uploads = 0;
  const bodies = [];
  const server = http.createServer(async (req, res) => {
    uploads++;
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    bodies.push(Buffer.concat(chunks));
    assert.equal(req.headers["x-api-key"], "synthetic-api");
    res.setHeader("content-type", "application/x-protobuf");
    res.end(encodeResponse());
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const origin = "http://127.0.0.1:" + server.address().port;
  const binding = {
    id: "00000000-0000-4000-8000-000000000001",
    endpoint: origin + "/v1/traces",
    approvedOrigin: origin,
    project: "fixture",
    loopbackTest: true,
    backendContract: "immutable-agent-v1",
  };
  const route = {
    id: "reference.fixture",
    binding,
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  };
  saveConfig(p, {
    schema_version: 1,
    profiles: {
      fixture: { binding, storage: "file", credentialRef: "fixture" },
    },
    routes: {
      "reference.fixture": {
        route,
        agent: "reference",
        surface: "fixture",
        profile: "fixture",
        transcriptConsent: true,
        activation: {
          installed: true,
          trusted: true,
          activated: false,
          restartRequired: false,
        },
      },
    },
  });
  await new FileSecretStore(p.credentials).set(
    "fixture",
    newCredentials({
      api_key: "synthetic-api",
      secret_key: "synthetic-secret",
    }),
  );
  const runtime = new Runtime(
    p,
    path.resolve(__dirname, "../dist/src/runtime/storage-child.js"),
    "0.1.0",
    new AdapterRegistry([reference]),
  );
  t.after(async () => {
    await runtime.stop();
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    process.chdir(previous);
    fs.rmSync(root, { recursive: true, force: true });
  });
  await runtime.start();
  const fact = reference.normalize(
    { schema: "fixture-v1", session: "s", turn: "t", id: "r", unit: "request" },
    { identityKey: Buffer.alloc(32), policyRevision: "1" },
  )[0];
  // IPC has a strict 25ms foreground budget; if ACK is lost, replay uses the same identity.
  let ack = await observe(p.socket, route.id, fact);
  if (!ack.durable) ack = await observe(p.socket, route.id, fact);
  assert.equal(ack.durable, true, JSON.stringify(ack));
  await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 1000 });
  assert.equal(uploads, 1);
  assert.ok(!Buffer.concat(bodies).includes(Buffer.from("synthetic-secret")));
  assert.equal(
    (await observe(p.socket, route.id, fact)).reason_code,
    "DUPLICATE",
  );
  await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 100 });
  assert.equal(uploads, 1);
  await runtimeRequest(p.socket, { operation: "disable", route: route.id });
  const disabled = await observe(p.socket, route.id, fact);
  assert.equal(disabled.durable, false);
  assert.equal(disabled.reason_code, "HELD_DISABLED");
  assert.equal(
    (await runtimeRequest(p.socket, { operation: "status" })).persistence,
    "unknown",
  );
});
