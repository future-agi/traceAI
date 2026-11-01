const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { saveConfig, loadConfig } = require("../dist/src/config.js");
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
  const empty = await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 1000 });
  assert.equal(empty.code, 0);
  assert.equal(empty.state, "complete");
  assert.equal(empty.counts.pending, 0);
  const fact = reference.normalize(
    { schema: "fixture-v1", session: "s", turn: "t", id: "r", unit: "request" },
    { identityKey: Buffer.alloc(32), policyRevision: "1" },
  )[0];
  // Shared-runner fsync can exceed the foreground budget. A timeout is neutral,
  // not proof of loss. The explicit off-path replay must establish one durable record.
  const foreground = await observe(p.socket, route.id, fact);
  assert.ok(
    foreground.durable || foreground.reason_code === "LOCAL_TIMEOUT",
    JSON.stringify(foreground),
  );
  const submission = { operation: "observe", route_ref: route.id, fact };
  const ack = await runtimeRequest(p.socket, submission);
  assert.equal(ack.durable, true, JSON.stringify(ack));
  const delivered = await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 1000 });
  assert.equal(delivered.code, 0);
  assert.equal(delivered.counts.pending, 0);
  assert.equal(uploads, 1);
  assert.ok(!Buffer.concat(bodies).includes(Buffer.from("synthetic-secret")));
  assert.equal(
    (await runtimeRequest(p.socket, submission)).reason_code,
    "DUPLICATE",
  );
  await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 100 });
  assert.equal(uploads, 1);
  // Editing only the profile cannot redirect the destination-bound outbox.
  const changed = loadConfig(p);
  changed.profiles.fixture.binding.endpoint = origin + "/unapproved-path";
  saveConfig(p, changed);
  await runtimeRequest(p.socket, { operation: "refresh" });
  const second = reference.normalize(
    {
      schema: "fixture-v1",
      session: "s",
      turn: "t",
      id: "r2",
      unit: "request",
    },
    { identityKey: Buffer.alloc(32), policyRevision: "1" },
  )[0];
  await runtimeRequest(p.socket, { ...submission, fact: second });
  const pending = await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 1000 });
  assert.equal(pending.code, 4);
  assert.equal(pending.counts.pending, 1);
  assert.equal(uploads, 1);
  changed.profiles.fixture.binding = binding;
  saveConfig(p, changed);
  await runtimeRequest(p.socket, { operation: "refresh" });
  await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 1000 });
  assert.equal(uploads, 2);
  const heldConfig = loadConfig(p);
  heldConfig.profiles.fixture.binding.endpoint = origin + "/unapproved-path";
  saveConfig(p, heldConfig);
  await runtimeRequest(p.socket, { operation: "refresh" });
  const third = reference.normalize(
    { schema: "fixture-v1", session: "s", turn: "t", id: "r3", unit: "request" },
    { identityKey: Buffer.alloc(32), policyRevision: "1" },
  )[0];
  await runtimeRequest(p.socket, { ...submission, fact: third });
  await runtimeRequest(p.socket, { operation: "disable", route: route.id });
  await assert.rejects(runtimeRequest(p.socket, submission), /HELD_DISABLED/);
  await runtimeRequest(p.socket, { operation: "refresh" });
  const reenabled = loadConfig(p);
  reenabled.profiles.fixture.binding = binding;
  reenabled.routes[route.id].route.enabled = true;
  reenabled.routes[route.id].route.epoch += 1;
  saveConfig(p, reenabled);
  await runtimeRequest(p.socket, { operation: "refresh" });
  const released = await runtimeRequest(p.socket, { operation: "flush", deadlineMs: 1000 });
  assert.equal(released.code, 0);
  assert.equal(released.counts.pending, 0);
  assert.equal(uploads, 3, "queued capture survives disable and is delivered after re-enable");
  assert.equal(
    (await runtimeRequest(p.socket, { operation: "status" })).persistence,
    "unknown",
  );
});
