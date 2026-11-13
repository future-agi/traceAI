import { request } from "node:https";
import { DisabledAttachmentManager } from "./disabled";
import { AttachmentManager } from "./manager";
import { attachmentEnabled } from "./policy";
import { ATTACHMENT_CHUNK_BYTES, AttachmentTransportError, createAttachmentHttpTransport } from "./transport-v1";
import type { AttachmentHttpClient } from "./transport-v1";
import type { AttachmentContentSource, AttachmentManagerOptions } from "./types";

type Ranged = Exclude<AttachmentContentSource, Uint8Array | Blob>;
function sizeOf(body: string | AttachmentContentSource | undefined): number {
  if (body === undefined) return 0;
  if (typeof body === "string") return Buffer.byteLength(body);
  if (body instanceof Uint8Array) return body.byteLength;
  return (body as { size: number }).size;
}
/** Node-only bounded transport. No keep-alive pool, redirect following, cookies or environment proxy. */
export const attachmentNodeHttpClient: AttachmentHttpClient = input => new Promise((resolve, reject) => {
  let settled = false;
  const fail = (code: "incomplete" | "unsupported_version" | "source_unavailable" = "incomplete", retryable = !input.signal.aborted) => {
    if (!settled) { settled = true; reject(new AttachmentTransportError(code, retryable)); }
  };
  try {
    const size = sizeOf(input.body);
    const req = request(input.url, { method: input.method, headers: { ...input.headers, ...(input.body === undefined ? {} : { "content-length": String(size) }) },
      signal: input.signal, agent: false }, response => {
      const status = response.statusCode ?? 0;
      const ok = status >= 200 && status < 300;
      // Success bodies up to 64 KiB; error bodies up to 4 KiB (parsed only as F01 Error).
      const limit = ok ? 65536 : 4096;
      const chunks: Buffer[] = []; let bytes = 0; let over = false;
      response.on("data", (chunk: Buffer) => {
        if (over) return;
        bytes += chunk.byteLength;
        if (bytes > limit) {
          over = true;
          if (ok) { fail("unsupported_version", false); response.destroy(); req.destroy(); return; }
          settled = true; resolve({ status, body: new Uint8Array(), retryAfter: response.headers["retry-after"] as string | undefined });
          response.destroy(); req.destroy(); return;
        }
        chunks.push(chunk);
      });
      response.on("error", () => fail());
      response.on("aborted", () => fail());
      response.on("end", () => {
        if (!settled) { settled = true; resolve({ status, body: Buffer.concat(chunks, bytes), retryAfter: response.headers["retry-after"] as string | undefined }); }
      });
    });
    req.on("socket", socket => socket.unref());
    req.on("error", () => fail());
    // Per-slice stall timeout on content PUTs.
    if (input.stallTimeoutMs) req.setTimeout(input.stallTimeoutMs, () => { req.destroy(); fail("incomplete", true); });
    void (async () => {
      const body = input.body;
      if (body === undefined) { req.end(); return; }
      if (typeof body === "string") { req.end(body); return; }
      const ranged = !(body instanceof Uint8Array) && !(typeof Blob !== "undefined" && body instanceof Blob) ? body as Ranged : undefined;
      for (let offset = 0; offset < size; offset += ATTACHMENT_CHUNK_BYTES) {
        if (input.signal.aborted || req.destroyed) return;
        const length = Math.min(ATTACHMENT_CHUNK_BYTES, size - offset);
        let chunk: Uint8Array;
        if (body instanceof Uint8Array) chunk = body.subarray(offset, offset + length);
        else if (ranged) {
          // check() before every slice: a changed owned file aborts the transfer.
          await ranged.check();
          chunk = await ranged.read(offset, length);
          if (chunk.byteLength !== length) throw new AttachmentTransportError("source_unavailable");
        } else chunk = new Uint8Array(await (body as Blob).slice(offset, offset + length).arrayBuffer());
        if (input.pacer) await input.pacer.take(chunk.byteLength, input.signal);
        if (input.signal.aborted || req.destroyed) return;
        if (!req.write(chunk)) await new Promise<void>(done => {
          const finish = () => { req.removeListener("drain", finish); req.removeListener("close", finish); done(); };
          req.once("drain", finish); req.once("close", finish);
        });
      }
      if (ranged) await ranged.check();
      req.end();
    })().catch(error => { req.destroy(); fail(error instanceof AttachmentTransportError ? error.code as "source_unavailable" : "incomplete", false); });
  } catch { fail(); }
});
export interface NodeAttachmentManagerOptions extends AttachmentManagerOptions { headers?: Readonly<Record<string, string>> }
/**
 * Provider setup passes only its resolved FI credentials, never a provider/model client's key. Returns the inert
 * DisabledAttachmentManager when attachments are OFF; `enabled` is resolved once here, from config then env.
 */
export function createNodeAttachmentManager(options: NodeAttachmentManagerOptions = {}): AttachmentManager {
  const environment = options.environment ?? (() => process.env);
  let env: Readonly<Record<string, string | undefined>>;
  try { env = environment(); } catch { env = { FI_ATTACHMENTS_ENABLED: "false" }; }
  if (!attachmentEnabled(options, env)) return new DisabledAttachmentManager();
  const endpoint = options.endpoint ?? env.FI_ATTACHMENTS_ENDPOINT;
  let transport = options.transport;
  if (!transport && endpoint) {
    try { transport = createAttachmentHttpTransport({ endpoint, headers: options.headers ?? {}, client: attachmentNodeHttpClient, stallTimeoutMs: options.requestTimeoutMs }); }
    catch { /* Unconfigured/untrusted origin remains fail closed without affecting tracer setup. */ }
  }
  return new AttachmentManager({ ...options, endpoint, environment, transport });
}
export { createRecordingFileScope } from "./node-sources";
export * from "./index";
