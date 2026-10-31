const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { exportBytes, retryDelay } = require("../dist/src/export/http.js");
const { validateBinding } = require("../dist/src/export/endpoint.js");
const {
  encodeResponse,
  decodeTraces,
  encodeFacts,
} = require("../dist/src/export/otlp.js");
const {
  startReceiver,
  preflightNative,
} = require("../dist/src/runtime/receiver.js");
const reference = require("../contract/reference.cjs");
const { AgentError } = require("../dist/src/security/files.js");
const credentials = {
  api_key: "synthetic-api",
  secret_key: "synthetic-secret",
};
const fact = reference.normalize(
  { schema: "fixture-v1", session: "s", turn: "t", id: "r", unit: "request" },
  { identityKey: Buffer.alloc(32), policyRevision: "1" },
)[0];
async function server(t, handler) {
  const s = http.createServer(handler);
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => s.close(r)));
  return s;
}
function binding(server) {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`,
    approvedOrigin: `http://127.0.0.1:${server.address().port}`,
    project: "fixture",
    loopbackTest: true,
    backendContract: "immutable-agent-v1",
  };
}
for (const status of [301, 302, 303, 307, 308])
  test(
    "authenticated " +
      status +
      " never follows cross-origin or same-origin Location",
    async (t) => {
      let target = 0,
        source = 0;
      const second = await server(t, (_req, res) => {
        target++;
        res.end();
      });
      const first = await server(t, (req, res) => {
        source++;
        assert.equal(req.headers["x-api-key"], credentials.api_key);
        res.writeHead(status, { Location: binding(second).endpoint }).end();
      });
      assert.deepEqual(
        await exportBytes(
          binding(first),
          Buffer.from("synthetic-body"),
          credentials,
        ),
        { state: "held", code: "HELD_REDIRECT" },
      );
      assert.equal(target, 0);
      assert.equal(source, 1);
      first.removeAllListeners("request");
      first.on("request", (_req, res) => {
        source++;
        res.writeHead(status, { Location: binding(first).endpoint }).end();
      });
      await exportBytes(
        binding(first),
        Buffer.from("synthetic-body"),
        credentials,
      );
      assert.equal(source, 2);
    },
  );
test("OTLP partial rejection is ambiguous; malformed success cannot become delivered", async (t) => {
  const s = await server(t, (_req, res) =>
    res
      .writeHead(200, { "content-type": "application/x-protobuf" })
      .end(encodeResponse(1)),
  );
  assert.equal(
    (await exportBytes(binding(s), Buffer.alloc(0), credentials)).state,
    "ambiguous",
  );
  s.removeAllListeners("request");
  s.on("request", (_req, res) =>
    res.writeHead(200, { "content-type": "text/html" }).end("not-otlp"),
  );
  assert.equal(
    (await exportBytes(binding(s), Buffer.alloc(0), credentials)).state,
    "ambiguous",
  );
});
for (const [status, state] of [
  [200, "accepted"],
  [401, "held"],
  [403, "held"],
  [413, "split"],
  [429, "retry"],
  [503, "retry"],
  [400, "held"],
])
  test("HTTP " + status + " delivery state is " + state, async (t) => {
    const s = await server(t, (_req, res) =>
      res.writeHead(status, { "retry-after": "999999" }).end(),
    );
    const result = await exportBytes(binding(s), Buffer.alloc(0), credentials);
    assert.equal(result.state, state);
    assert.ok((result.retryAfterMs ?? 0) <= 60000);
  });
test("destination rejects plaintext remote URLs, credentials, query, fragments and unapproved origin", () => {
  const b = {
    id: "00000000-0000-4000-8000-000000000001",
    endpoint: "https://api.futureagi.com/tracer/v1/traces",
    approvedOrigin: "https://api.futureagi.com",
    project: "fixture",
  };
  assert.equal(validateBinding(b).href, b.endpoint);
  for (const endpoint of [
    "http://example.com/v1/traces",
    "https://user:pass@api.futureagi.com/v1/traces",
    b.endpoint + "?key=bad",
    b.endpoint + "#bad",
    "https://other.test/v1/traces",
  ])
    assert.throws(() => validateBinding({ ...b, endpoint }));
  assert.ok(retryDelay(999, 999999, () => 1) <= 60000);
});
test("unknown backend holds without opening a network connection", async () => {
  const b = {
    id: "00000000-0000-4000-8000-000000000001",
    endpoint: "http://127.0.0.1:1/v1/traces",
    approvedOrigin: "http://127.0.0.1:1",
    project: "fixture",
    loopbackTest: true,
  };
  assert.equal(
    (await exportBytes(b, Buffer.alloc(0), credentials)).code,
    "HELD_BACKEND_CONTRACT",
  );
});
test("native conflict refusal precedes listener creation; receiver rejects browser and other signals", async (t) => {
  assert.throws(
    () => preflightNative({ state: "unowned-active" }),
    /NATIVE_DESTINATION_CONFLICT/,
  );
  assert.throws(() => preflightNative({ state: "shared-signal-conflict" }));
  await assert.rejects(
    startReceiver({
      coexistence: { state: "owned-or-none" },
      normalize: () => ({ ...fact, source: "native" }),
      admit: async () => {},
    }),
    /HELD_LOCAL_AUTH/,
  );
  const received = [];
  const s = await startReceiver({
    coexistence: { state: "external-fanout", validated: true },
    singleUserApproved: true,
    normalize: () => ({ ...fact, source: "native" }),
    admit: async (f) => received.push(f),
  });
  t.after(() => new Promise((r) => s.close(r)));
  const url = binding(s).endpoint;
  const body = encodeFacts([fact], binding(s), "0.1.0");
  let response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/x-protobuf",
      Origin: "https://untrusted.test",
    },
    body,
  });
  assert.equal(response.status, 403);
  response = await fetch(url.replace("/traces", "/logs"), {
    method: "POST",
    headers: { "content-type": "application/x-protobuf" },
    body,
  });
  assert.equal(response.status, 404);
  response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/x-protobuf" },
    body,
  });
  assert.equal(response.status, 200);
  assert.equal(received.length, 1);
  const wire = decodeTraces(encodeFacts(received, binding(s), "0.1.0"));
  const attributes = wire.resourceSpans[0].scopeSpans[0].spans[0].attributes;
  assert.equal(
    attributes.find((x) => x.key === "gen_ai.usage.input_tokens").value
      .doubleValue,
    100,
  );
});

for (const type of ["application/json", "application/x-protobuf"])
  for (const code of [
    "STORAGE_UNAVAILABLE",
    "PAUSED_CHECKPOINT",
    "PAUSED_LOGICAL_QUOTA",
    "UNEXPECTED",
  ])
    test(`native ${type} retries ${code} after partial durable admission`, async (t) => {
      const second = reference.normalize(
        {
          schema: "fixture-v1",
          session: "s",
          turn: "t",
          id: "second",
          unit: "request",
        },
        { identityKey: Buffer.alloc(32), policyRevision: "1" },
      )[0];
      const facts = [fact, second].map((f) => ({ ...f, source: "native" }));
      const durable = new Set();
      let fail = true,
        duplicates = 0;
      const s = await startReceiver({
        coexistence: { state: "owned-or-none" },
        singleUserApproved: true,
        normalize: (span) =>
          facts.find(
            (f) =>
              Buffer.from(span.spanId, "base64").toString("hex") === f.spanId,
          ),
        admit: async (f) => {
          if (fail && f.sourceWorkId === second.sourceWorkId) {
            if (code === "UNEXPECTED") throw new Error("private error text");
            throw new AgentError(code);
          }
          if (durable.has(f.sourceWorkId)) duplicates++;
          durable.add(f.sourceWorkId);
        },
      });
      t.after(() => new Promise((r) => s.close(r)));
      const wire = encodeFacts(facts, binding(s), "0.1.0");
      const send = () =>
        fetch(binding(s).endpoint, {
          method: "POST",
          headers: { "content-type": type },
          body:
            type === "application/json"
              ? JSON.stringify(decodeTraces(wire))
              : wire,
        });
      const first = await send();
      assert.equal(first.status, 503);
      assert.ok(!(await first.text()).includes("private"));
      assert.equal(durable.size, 1);
      fail = false;
      const retry = await send();
      assert.equal(retry.status, 200);
      await retry.arrayBuffer();
      assert.equal(durable.size, 2);
      assert.equal(duplicates, 1);
    });

for (const type of ["application/json", "application/x-protobuf"])
  for (const reject of [false, true])
    test(`native ${type} responds in-kind for ${reject ? "partial rejection" : "acceptance"}`, async (t) => {
      let admits = 0;
      const s = await startReceiver({
        coexistence: { state: "owned-or-none" },
        singleUserApproved: true,
        normalize: () => ({ ...fact, source: "native" }),
        admit: async () => {
          admits++;
          if (reject) throw new AgentError("HELD_IMMUTABLE_CONFLICT", 3);
        },
      });
      t.after(() => new Promise((r) => s.close(r)));
      const wire = encodeFacts([fact], binding(s), "0.1.0");
      const json = decodeTraces(wire);
      const span = json.resourceSpans[0].scopeSpans[0].spans[0];
      for (const key of ["traceId", "spanId", "parentSpanId"])
        if (span[key])
          span[key] = Buffer.from(span[key], "base64").toString("hex");
      const response = await fetch(binding(s).endpoint, {
        method: "POST",
        headers: { "content-type": type },
        body: type === "application/json" ? JSON.stringify(json) : wire,
      });
      assert.equal(response.status, 200);
      assert.equal(response.headers.get("content-type"), type);
      assert.equal(admits, 1);
      if (type === "application/json")
        assert.deepEqual(
          await response.json(),
          reject ? { partialSuccess: { rejectedSpans: "1" } } : {},
        );
      else
        assert.equal(
          require("../dist/src/export/otlp.js").decodeRejections(
            Buffer.from(await response.arrayBuffer()),
          ),
          reject ? 1 : 0,
        );
    });
