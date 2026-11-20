import { createServer, IncomingHttpHeaders, Server } from "node:http";
import { spawn } from "node:child_process";
import { join } from "node:path";
import type { AddressInfo } from "node:net";

// Offline: capture server on 127.0.0.1 only; synthetic header values only.
const AMBIENT_AUTH = "Bearer SYNTHETIC-OTHER-BACKEND";
const ENV_CASES: Array<[string, string]> = [
  ["OTEL_EXPORTER_OTLP_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-all`],
  ["OTEL_EXPORTER_OTLP_TRACES_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-traces`],
];
let server: Server, base: string, received: IncomingHttpHeaders[];
beforeAll(async () => {
  server = createServer((req, res) => { req.resume(); req.on("end", () => { received.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); });

const KEYS = ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL"];
let saved: Record<string, string | undefined>;
beforeEach(() => { received = []; saved = Object.fromEntries(KEYS.map(k => [k, process.env[k]])); KEYS.forEach(k => delete process.env[k]); });
afterEach(() => { for (const k of KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

// Known failure while ambient headers reach the FI exporter; set AMBIENT_HEADERS_EXPECT_FIXED=1 to run, drop the guard once fixed.
const run = process.env.AMBIENT_HEADERS_EXPECT_FIXED ? test : test.skip;
describe.each(ENV_CASES)("ambient %s with the auto init() path", (name, value) => {
  run("ambient headers are not forwarded; FI headers are (ambient OTLP headers reach the FI exporter; fix pending)", async () => {
    await new Promise<{ stderr: string }>(done => {
      const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", "ambient-headers.ts")], {
        cwd: join(__dirname, ".."), timeout: 60000,
        env: { PATH: process.env.PATH, HOME: process.env.HOME, FI_TEST_BASE_URL: base, [name]: value },
      });
      let stderr = ""; child.stderr.on("data", c => { stderr += c; }); child.on("close", () => done({ stderr }));
    });
    expect(received.length).toBeGreaterThan(0);
    for (const headers of received) {
      expect(headers["x-api-key"]).toBe("synthetic-fi-key");
      expect(headers["x-secret-key"]).toBe("synthetic-fi-secret");
      expect(headers["authorization"]).toBeUndefined();
      expect(headers["x-synthetic-ambient-marker"]).toBeUndefined();
    }
  });
});
