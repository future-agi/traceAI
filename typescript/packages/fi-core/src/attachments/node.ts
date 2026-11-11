import { request } from "node:https";
import { AttachmentManager } from "./manager";
import { AttachmentTransportError, createAttachmentHttpTransport } from "./transport-v1";
import type { AttachmentHttpClient } from "./transport-v1";
import type { AttachmentManagerOptions } from "./types";

/** Node-only bounded transport. No keep-alive pool, redirect following, cookies or environment proxy. */
export const attachmentNodeHttpClient: AttachmentHttpClient = input => new Promise((resolve, reject) => {
  let settled = false;
  const fail = () => { if (!settled) { settled = true; reject(new AttachmentTransportError("failed", !input.signal.aborted)); } };
  try {
    const size = typeof input.body === "string" ? Buffer.byteLength(input.body) : input.body instanceof Uint8Array ? input.body.byteLength : input.body.size;
    const req = request(input.url, { method: input.method, headers: { ...input.headers, "content-length": String(size) },
      signal: input.signal, agent: false }, response => {
      if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
        settled = true;
        resolve({ status: response.statusCode ?? 0, body: new Uint8Array(), retryAfter: response.headers["retry-after"] });
        response.destroy(); req.destroy(); return;
      }
      const chunks: Buffer[] = []; let bytes = 0;
      response.on("data", (chunk: Buffer) => {
        bytes += chunk.byteLength;
        if (bytes > 65536) { settled = true; reject(new AttachmentTransportError("failed")); response.destroy(); req.destroy(); return; }
        chunks.push(chunk);
      });
      response.on("error", fail);
      response.on("aborted", fail);
      response.on("end", () => {
        if (!settled) { settled = true; resolve({ status: response.statusCode ?? 0, body: Buffer.concat(chunks, bytes), retryAfter: response.headers["retry-after"] }); }
      });
    });
    req.on("socket", socket => socket.unref());
    req.on("error", fail);
    // A Blob is immutable and read independently, one bounded slice at a time.
    void (async () => {
      if (typeof input.body === "string") { req.end(input.body); return; }
      for (let offset = 0; offset < size; offset += 256 * 1024) {
        if (input.signal.aborted || req.destroyed) return;
        const chunk = input.body instanceof Uint8Array ? input.body.subarray(offset, offset + 256 * 1024) :
          new Uint8Array(await input.body.slice(offset, offset + 256 * 1024).arrayBuffer());
        if (input.signal.aborted || req.destroyed) return;
        if (!req.write(chunk)) await new Promise<void>(done => {
          const finish = () => { req.removeListener("drain", finish); req.removeListener("close", finish); done(); };
          req.once("drain", finish); req.once("close", finish);
        });
      }
      req.end();
    })().catch(() => { req.destroy(); fail(); });
  } catch { fail(); }
});
export interface NodeAttachmentManagerOptions extends AttachmentManagerOptions { headers?: Readonly<Record<string, string>> }
/** Provider setup passes only its resolved FI credentials, never a provider/model client's key. */
export function createNodeAttachmentManager(options: NodeAttachmentManagerOptions = {}): AttachmentManager {
  const endpoint = options.endpoint ?? process.env.FI_ATTACHMENTS_ENDPOINT;
  let transport = options.transport;
  if (!transport && endpoint) {
    try { transport = createAttachmentHttpTransport({ endpoint, headers: options.headers ?? {}, client: attachmentNodeHttpClient }); }
    catch { /* Unconfigured/untrusted origin remains fail closed without affecting tracer setup. */ }
  }
  return new AttachmentManager({ ...options, endpoint, environment: options.environment ?? (() => process.env), transport });
}
export * from "./index";
