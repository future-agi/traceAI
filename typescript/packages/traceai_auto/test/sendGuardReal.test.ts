import { spawn } from "node:child_process";
import http from "node:http";
import net, { AddressInfo } from "node:net";
import { join } from "node:path";

/**
 * @traceai/auto sends through the guard, measured at the sockets of real listeners in the parent process: a real sink that records the
 * request, and a listener that stands for a proxy and counts accepted connections and bytes. The child process runs init() for real.
 */
function run(env: Record<string, string>): Promise<{ stdout: string; stderr: string }> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", "real-send.ts")], {
      cwd: join(__dirname, ".."), timeout: 60000,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, FI_PROJECT_NAME: "real-send", FI_API_KEY: "synthetic-fi-key-0001", FI_SECRET_KEY: "synthetic-fi-secret-0001", ...env },
    });
    let stdout = "", stderr = ""; child.stdout.on("data", c => { stdout += c; }); child.stderr.on("data", c => { stderr += c; });
    child.on("close", () => done({ stdout, stderr }));
  });
}
const closers: Array<() => Promise<void>> = [];
afterEach(async () => { for (const close of closers.splice(0)) await close(); });
async function proxyCounter(): Promise<{ url: string; connections: number; bytes: number }> {
  const state = { url: "", connections: 0, bytes: 0 };
  const sockets = new Set<net.Socket>();
  const server = net.createServer(socket => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("error", () => {}); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  closers.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
async function sink(): Promise<{ port: number; requests: http.IncomingHttpHeaders[]; bytes: number }> {
  const state = { port: 0, requests: [] as http.IncomingHttpHeaders[], bytes: 0 };
  const sockets = new Set<net.Socket>();
  const server = http.createServer((req, res) => { req.resume(); req.on("end", () => { state.requests.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); }); });
  server.on("connection", socket => { sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.port = (server.address() as AddressInfo).port;
  closers.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
const PROXY_VARIABLES = (url: string) => ({ HTTP_PROXY: url, http_proxy: url, ALL_PROXY: url, HTTPS_PROXY: url, https_proxy: url, grpc_proxy: url });

describe("init() through the guard, with real sockets", () => {
  test("(pin) a plaintext self-hosted base URL with the opt-in and every proxy variable set goes direct: the sink receives the keys, the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await proxyCounter();
    // 127.1 is not the literal loopback: it needs the opt-in, and the connection still reaches this machine.
    const { stdout, stderr } = await run({ FI_TEST_BASE_URL: `http://127.1:${target.port}`, FI_TEST_ALLOW: "1", ...PROXY_VARIABLES(proxy.url) });
    expect(stdout).toContain("INIT_OK");
    expect(target.requests.length).toBeGreaterThanOrEqual(1);
    expect(target.requests[0]["x-api-key"]).toBe("synthetic-fi-key-0001");
    expect(proxy.connections).toBe(0); expect(proxy.bytes).toBe(0);
    expect(stderr).not.toMatch(/synthetic-fi/);
  });
  test("(pin) without the opt-in nothing is sent: the sink and the proxy see 0 bytes, and the message names the setting", async () => {
    const target = await sink(), proxy = await proxyCounter();
    const { stdout, stderr } = await run({ FI_TEST_BASE_URL: `http://127.1:${target.port}`, ...PROXY_VARIABLES(proxy.url) });
    expect(stdout).toContain("INIT_NONE");
    expect(target.bytes).toBe(0); expect(proxy.bytes).toBe(0); expect(proxy.connections).toBe(0);
    expect(stderr).toContain("FI_ALLOW_INSECURE_HTTP");
    expect(stderr).not.toMatch(/synthetic-fi|127\.1/);
  });
  test("(pin) a loopback base URL with every proxy variable set goes direct and the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await proxyCounter();
    const { stdout } = await run({ FI_TEST_BASE_URL: `http://127.0.0.1:${target.port}`, ...PROXY_VARIABLES(proxy.url) });
    expect(stdout).toContain("INIT_OK");
    expect(target.requests.length).toBeGreaterThanOrEqual(1);
    expect(proxy.connections).toBe(0); expect(proxy.bytes).toBe(0);
  });
});
