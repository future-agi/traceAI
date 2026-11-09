const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { postOtlp } = require("../../../dist/src/index.js");
for (const [name, body, type, expected, count] of [
  ["empty success", Buffer.alloc(0), "application/x-protobuf", "ACCEPTED"],
  [
    "partial success",
    Buffer.from([0x0a, 0x02, 0x08, 0x01]),
    "application/x-protobuf",
    "PARTIAL_REJECTION",
    1,
  ],
  [
    "malformed response",
    Buffer.from([0xff]),
    "application/x-protobuf",
    "UNVERIFIED",
  ],
  ["wrong encoding", Buffer.from("{}"), "application/json", "UNVERIFIED"],
])
  test("protobuf delivery: " + name, async (t) => {
    const server = http.createServer((q, r) => {
      assert.equal(q.headers["content-type"], "application/x-protobuf");
      q.resume();
      q.on("end", () => {
        r.setHeader("content-type", type);
        r.end(body);
      });
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    t.after(() => new Promise((r) => server.close(r)));
    const result = await postOtlp({
      endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`,
      credentials: { apiKey: "dummy-api", secretKey: "dummy-secret" },
      projectName: "test",
      body: Buffer.alloc(0),
      protocol: "http/protobuf",
    });
    assert.equal(result.code, expected);
    if (count !== undefined) assert.equal(result.rejectedSpans, count);
  });
