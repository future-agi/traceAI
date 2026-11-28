import path from "node:path";
import { runChild, BUILT_GUARD, throwawayCertificate } from "./helpers/realSockets";

/**
 * Every response body of fi-core's own transport has an error listener, and a body nobody reads is dropped: a collector that answers with
 * headers and then resets the socket (or stalls) must not raise an uncaught exception or an unhandled rejection in the host application, and
 * must not keep the connection. Real sockets and a child process of the built package: the child holds the misbehaving collector itself and
 * counts process-level error events.
 */
const INDEX = path.join(path.dirname(BUILT_GUARD), "index");
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };

const CHILD = `
  const net = require("node:net");
  const lib = require(${JSON.stringify(INDEX)});
  const events = [];
  process.on("uncaughtException", error => events.push("UNCAUGHT " + (error && (error.code || error.message))));
  process.on("unhandledRejection", error => events.push("UNHANDLED " + (error && (error.code || error.message))));
  const scenario = process.env.SCENARIO, status = process.env.STATUS || "200", mode = process.env.BODY;
  const span = { name: "x", kind: 0, spanContext: () => ({ traceId: "0".repeat(31) + "1", spanId: "0".repeat(15) + "1", traceFlags: 1 }), startTime: [0, 0], endTime: [0, 1], status: { code: 0 }, attributes: {}, links: [], events: [],
    duration: [0, 1], ended: true, resource: { attributes: {} }, instrumentationLibrary: { name: "t" }, droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0 };
  const closed = []; const sockets = new Set(); let requests = 0;
  const server = net.createServer(socket => {
    sockets.add(socket); socket.on("error", () => {}); socket.on("close", () => { sockets.delete(socket); closed.push(Date.now()); });
    let seen = "", answered = false;
    socket.on("data", chunk => {
      seen += chunk.toString("latin1");
      if (answered || !seen.includes("\\r\\n\\r\\n")) return;
      answered = true; requests++; socket.carried = true;
      if (mode === "silent") return;
      socket.write("HTTP/1.1 " + status + " X\\r\\ncontent-length: 1000\\r\\ncontent-type: application/json\\r\\n\\r\\n{\\"par");
      if (mode === "reset") setTimeout(() => socket.resetAndDestroy(), 50);
    });
  });
  const wait = ms => new Promise(done => setTimeout(done, ms));
  const tag = { type: "observation_span", value: "llm", eval_name: "Chunk_Attribution", config: {}, custom_eval_name: "Chunk_Attribution", mapping: {}, model: "turing_small", toDict: () => ({ eval_name: "Chunk_Attribution", custom_eval_name: "Chunk_Attribution" }) };
  server.listen(0, "127.0.0.1", async () => {
    const url = "http://127.0.0.1:" + server.address().port;
    try {
      if (scenario === "export") {
        const exporter = new lib.HTTPSpanExporter({ endpoint: url + "/t", headers: ${JSON.stringify(KEYS)}, allowInsecureHttp: true });
        const result = await new Promise(done => exporter.export([span], done));
        console.log("RESULT " + result.code);
      } else if (scenario === "eval-config" || scenario === "eval-template") {
        process.env.FI_API_KEY = ${JSON.stringify(KEYS["x-api-key"])}; process.env.FI_SECRET_KEY = ${JSON.stringify(KEYS["x-secret-key"])}; process.env.FI_BASE_URL = url;
        const value = scenario === "eval-config"
          ? await lib.checkCustomEvalConfigExists("p", [tag], undefined, false, {})
          : await lib.checkCustomEvalTemplateExists("t", undefined, false, {});
        console.log("RESULT " + JSON.stringify(value));
      } else if (scenario === "timeout") {
        const exporter = lib.createSpanExporter({ endpoint: url + "/t", headers: ${JSON.stringify(KEYS)}, timeoutMillis: 300, allowInsecureHttp: true });
        const result = await new Promise(done => exporter.export([span], done));
        console.log("RESULT " + result.code + " " + (result.error && result.error.message));
        await exporter.shutdown();
      }
    } catch (error) { console.log("THROWN " + (error && error.message)); }
    // An unread body is dropped, so the client closes the connection that carried the request instead of holding it until the body timeout.
    // (undici may open one more, idle connection after that; it carries no request and is not counted.)
    const carrying = () => [...sockets].filter(socket => socket.carried).length;
    const started = Date.now();
    while (carrying() > 0 && Date.now() - started < 3000) await wait(25);
    await wait(300);
    console.log("REQUESTS " + requests);
    console.log("SOCKETS_OPEN " + carrying());
    console.log("EVENTS " + JSON.stringify(events));
    process.exit(0);
  });
`;

const run = (scenario: string, status: string, body: string): Promise<string> =>
  runChild(CHILD, [], { SCENARIO: scenario, STATUS: status, BODY: body, FI_ALLOW_INSECURE_HTTP: "1" });

describe("a response body nobody reads cannot crash the host or hold its connection", () => {
  test.each([
    ["export", "200"], ["export", "500"], ["eval-config", "500"], ["eval-template", "500"],
  ])("%s answering %s and then resetting the socket mid-body: no uncaught exception, no unhandled rejection, the socket is released", async (scenario, status) => {
    const output = await run(scenario, status, "reset");
    expect(output).toContain("REQUESTS 1");
    expect(output).toContain("EVENTS []");
    expect(output).toContain("SOCKETS_OPEN 0");
    expect(output).not.toMatch(/UNCAUGHT|UNHANDLED|THROWN/);
  }, 40000);
  test.each([
    ["export", "200"], ["export", "500"], ["eval-config", "500"], ["eval-template", "500"],
  ])("%s answering %s with a body that stalls: the body is dropped and the client closes the connection", async (scenario, status) => {
    const output = await run(scenario, status, "stall");
    expect(output).toContain("EVENTS []");
    expect(output).toContain("SOCKETS_OPEN 0");
  }, 40000);
  test("a stalled export reports the same 'Request Timeout' as before", async () => {
    const output = await run("timeout", "200", "silent");
    expect(output).toMatch(/RESULT 1 Request Timeout/);
    expect(output).toContain("EVENTS []");
  }, 40000);
});

describe("neither a direct nor a tunnelled https send loads undici's global module, package index or fetch", () => {
  test("require.cache holds none of them after both sends", async () => {
    const cert = throwawayCertificate();
    try {
      const script = `
        const https = require("node:https"), net = require("node:net"), http = require("node:http"), fs = require("node:fs");
        const guard = require(${JSON.stringify(BUILT_GUARD)});
        const keys = ${JSON.stringify(KEYS)};
        const intended = { keys: new Map(Object.entries(keys)), values: Object.values(keys) };
        const target = https.createServer({ key: process.env.KEY, cert: process.env.CERT }, (req, res) => { req.resume(); req.on("end", () => res.end("{}")); });
        const proxy = http.createServer((_q, res) => { res.statusCode = 502; res.end(); });
        proxy.on("connect", (req, client) => { const [host, port] = req.url.split(":"); const up = net.connect(Number(port), host, () => { client.write("HTTP/1.1 200 OK\\r\\n\\r\\n"); up.pipe(client); client.pipe(up); }); up.on("error", () => client.destroy()); client.on("error", () => up.destroy()); });
        const listen = server => new Promise(done => server.listen(0, "127.0.0.1", done));
        (async () => {
          await listen(target); await listen(proxy);
          const url = "https://localhost:" + target.address().port + "/x";
          const direct = await guard.guardedFetch({ url, init: { method: "POST", headers: keys }, intended });
          console.log("DIRECT " + direct.status);
          const tunnelled = await guard.guardedFetch({ url, init: { method: "POST", headers: keys }, intended, httpsProxy: "http://127.0.0.1:" + proxy.address().port });
          console.log("TUNNELLED " + tunnelled.status);
          const loaded = Object.keys(require.cache).filter(name => /undici/.test(name));
          const bad = loaded.filter(name => /undici[\\\\/]index\\.js$|lib[\\\\/]global\\.js$|lib[\\\\/]web[\\\\/]fetch[\\\\/]/.test(name));
          console.log("UNDICI_MODULES " + loaded.length + " BAD " + JSON.stringify(bad));
          process.exit(0);
        })();
      `;
      const output = await runChild(script, [], { KEY: cert.key, CERT: cert.cert, NODE_EXTRA_CA_CERTS: cert.caFile });
      expect(output).toContain("DIRECT 200");
      expect(output).toContain("TUNNELLED 200");
      expect(output).toContain("BAD []");
      expect(output).toMatch(/UNDICI_MODULES [1-9]/);
    } finally { cert.dispose(); }
  }, 40000);
});
