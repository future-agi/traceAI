import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import net, { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";

/** Real listeners for the send-guard cells: they count accepted connections and bytes on their net.Server sockets. */
export interface Counter { port: number; url: string; connections: number; bytes: number }
export interface Sink extends Counter { requests: Array<{ url: string; headers: http.IncomingHttpHeaders }> }
export const cleanups: Array<() => Promise<void> | void> = [];
export async function runCleanups(): Promise<void> { for (const fn of cleanups.splice(0)) await fn(); }

export async function counter(host = "127.0.0.1"): Promise<Counter> {
  const state: Counter = { port: 0, url: "", connections: 0, bytes: 0 };
  const sockets = new Set<net.Socket>();
  const server = net.createServer(socket => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("error", () => {}); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(0, host, done));
  state.port = (server.address() as AddressInfo).port; state.url = `http://${host}:${state.port}`;
  cleanups.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
export async function sink(options: { tls?: { key: string; cert: string }; respond?: (res: http.ServerResponse) => void } = {}): Promise<Sink> {
  const state: Sink = { port: 0, url: "", connections: 0, bytes: 0, requests: [] };
  const handler = (req: http.IncomingMessage, res: http.ServerResponse) => {
    req.resume(); req.on("end", () => { state.requests.push({ url: req.url ?? "", headers: req.headers }); if (options.respond) options.respond(res); else { res.setHeader("content-type", "application/json"); res.end("{}"); } });
  };
  const server = options.tls ? https.createServer(options.tls, handler) : http.createServer(handler);
  const sockets = new Set<net.Socket>();
  server.on(options.tls ? "secureConnection" : "connection", (socket: net.Socket) => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.port = (server.address() as AddressInfo).port; state.url = `${options.tls ? "https" : "http"}://127.0.0.1:${state.port}`;
  cleanups.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
/** A CONNECT proxy that tunnels every request to `upstream`, recording what it was asked for. */
export async function connectProxy(upstreamPort: number): Promise<Counter & { connects: string[] }> {
  const state = { port: 0, url: "", connections: 0, bytes: 0, connects: [] as string[] };
  const sockets = new Set<net.Socket>();
  const server = http.createServer((_req, res) => { res.statusCode = 502; res.end(); });
  server.on("connection", (socket: net.Socket) => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("error", () => {}); });
  server.on("connect", (req, clientSocket) => {
    state.connects.push(req.url ?? "");
    const upstream = net.connect(upstreamPort, "127.0.0.1", () => { clientSocket.write("HTTP/1.1 200 Connection established\r\n\r\n"); upstream.pipe(clientSocket); clientSocket.pipe(upstream); });
    upstream.on("error", () => clientSocket.destroy()); clientSocket.on("error", () => upstream.destroy());
    clientSocket.on("data", chunk => { state.bytes += chunk.length; });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.port = (server.address() as AddressInfo).port; state.url = `http://127.0.0.1:${state.port}`;
  cleanups.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
export const redirectingConnect = (host: string, port: number) => (_options: unknown, callback: (error: Error | null, socket?: net.Socket) => void) => {
  const socket = net.connect({ host, port }); socket.once("connect", () => callback(null, socket)); socket.once("error", error => callback(error));
};
/** A throwaway CA-signed certificate pair for localhost, and the CA certificate a child process trusts through NODE_EXTRA_CA_CERTS. */
export function throwawayCertificate(): { key: string; cert: string; caFile: string; dispose(): void } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fi-guard-ca-"));
  const run = (args: string[]) => { const r = spawnSync("openssl", args, { encoding: "utf8" }); if (r.status !== 0) throw new Error(`openssl failed: ${r.stderr}`); };
  run(["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=fi test ca", "-keyout", path.join(dir, "ca.key"), "-out", path.join(dir, "ca.pem"),
    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign"]);
  run(["req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-keyout", path.join(dir, "leaf.key"), "-out", path.join(dir, "leaf.csr")]);
  fs.writeFileSync(path.join(dir, "ext.cnf"), "subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n");
  run(["x509", "-req", "-in", path.join(dir, "leaf.csr"), "-CA", path.join(dir, "ca.pem"), "-CAkey", path.join(dir, "ca.key"), "-CAcreateserial", "-days", "2", "-extfile", path.join(dir, "ext.cnf"), "-out", path.join(dir, "leaf.pem")]);
  return { key: fs.readFileSync(path.join(dir, "leaf.key"), "utf8"), cert: fs.readFileSync(path.join(dir, "leaf.pem"), "utf8"), caFile: path.join(dir, "ca.pem"), dispose: () => fs.rmSync(dir, { recursive: true, force: true }) };
}
/** Run a script in a child process of the built package, asynchronously (the servers of the test live in this process). */
export function runChild(script: string, args: string[], env: Record<string, string>): Promise<string> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["-e", script, ...args], { env: { PATH: process.env.PATH ?? "", ...env }, timeout: 30000 });
    let text = ""; child.stdout.on("data", chunk => { text += chunk; }); child.stderr.on("data", chunk => { text += chunk; }); child.on("close", () => done(text));
  });
}
export const BUILT_GUARD = path.join(__dirname, "..", "..", "..", "dist", "src", "fi-send-guard");
export const UNDICI = path.dirname(require.resolve("undici"));
