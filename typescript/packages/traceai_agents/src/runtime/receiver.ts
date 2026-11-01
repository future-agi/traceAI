import { createServer, type Server } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { decodeTraces, encodeResponse } from "../export/otlp.js";
import { AgentError } from "../security/files.js";
import { minimize, type CanonicalFact } from "@traceai/agent-core";
export type Coexistence =
  | { state: "owned-or-none" }
  | { state: "unowned-active" }
  | { state: "shared-signal-conflict" }
  | { state: "external-fanout"; validated: boolean }
  | {
      state: "process-replace";
      acknowledged: boolean;
      signalsIsolated: boolean;
    };
export function preflightNative(mode: Coexistence): void {
  if (mode.state === "owned-or-none") return;
  if (mode.state === "external-fanout" && mode.validated) return;
  if (
    mode.state === "process-replace" &&
    mode.acknowledged &&
    mode.signalsIsolated
  )
    return;
  throw new AgentError("NATIVE_DESTINATION_CONFLICT", 6);
}
export interface ReceiverOptions {
  coexistence: Coexistence;
  token?: string;
  singleUserApproved?: boolean;
  normalize: (span: unknown) => CanonicalFact;
  admit: (fact: CanonicalFact) => Promise<unknown>;
}
/** No FI keys; only traces. Existing vendor signals must remain in their original collector. */
export async function startReceiver(options: ReceiverOptions): Promise<Server> {
  preflightNative(options.coexistence);
  if (!options.token && !options.singleUserApproved)
    throw new AgentError("HELD_LOCAL_AUTH", 3);
  let active = 0;
  const server = createServer(async (req, res) => {
    if (++active > 4) {
      active--;
      req.resume();
      res.writeHead(503).end();
      return;
    }
    let finished = false;
    const release = () => {
      if (!finished) {
        finished = true;
        active--;
      }
    };
    res.once("close", release);
    res.once("finish", release);
    const address = server.address();
    const expected =
      typeof address === "object" && address ? "127.0.0.1:" + address.port : "";
    const provided = req.headers.authorization;
    const token = "Bearer " + options.token;
    const auth =
      !options.token ||
      (typeof provided === "string" &&
        Buffer.byteLength(provided) === Buffer.byteLength(token) &&
        timingSafeEqual(Buffer.from(provided), Buffer.from(token)));
    if (req.headers.origin || req.headers.host !== expected || !auth) {
      res.writeHead(403).end();
      req.resume();
      return;
    }
    if (req.method !== "POST" || req.url !== "/v1/traces") {
      res.writeHead(404).end();
      req.resume();
      return;
    }
    const type = req.headers["content-type"]?.split(";")[0];
    if (
      !["application/json", "application/x-protobuf"].includes(type ?? "") ||
      (req.headers["content-encoding"] &&
        req.headers["content-encoding"] !== "identity")
    ) {
      res.writeHead(415).end();
      req.resume();
      return;
    }
    let size = 0;
    const chunks: Buffer[] = [];
    try {
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1_048_576) {
          res.writeHead(413).end();
          req.destroy();
          return;
        }
        chunks.push(Buffer.from(chunk));
      }
      const bytes = Buffer.concat(chunks);
      const data =
        type === "application/json"
          ? JSON.parse(bytes.toString())
          : decodeTraces(bytes);
      if (
        !data ||
        !Array.isArray(data.resourceSpans) ||
        data.resourceSpans.length > 256
      )
        throw new Error();
      const spans: unknown[] = [];
      for (const resource of data.resourceSpans) {
        if (
          !Array.isArray(resource.scopeSpans) ||
          resource.scopeSpans.length > 256
        )
          throw new Error();
        for (const scope of resource.scopeSpans) {
          if (!Array.isArray(scope.spans)) throw new Error();
          spans.push(...scope.spans);
          if (spans.length > 256) throw new Error();
        }
      }
      // Normalize every span before admitting any; raw resources/events/status text never persist.
      const facts = spans.map((span: any) => {
        const fact = minimize(options.normalize(span));
        const id = (value: unknown, length: number): string => {
          if (typeof value !== "string") throw new Error("INVALID_NATIVE_ID");
          const decoded = new RegExp(`^[a-fA-F0-9]{${length * 2}}$`).test(value)
            ? value.toLowerCase()
            : Buffer.from(value, "base64").toString("hex");
          if (decoded.length !== length * 2 || /^0+$/.test(decoded))
            throw new Error("INVALID_NATIVE_ID");
          return decoded;
        };
        if (
          fact.source !== "native" ||
          fact.traceId !== id(span.traceId, 16) ||
          fact.spanId !== id(span.spanId, 8)
        )
          throw new Error("NATIVE_ID_CHANGED");
        const parent = span.parentSpanId ? id(span.parentSpanId, 8) : undefined;
        if (parent !== fact.parentSpanId)
          throw new Error("NATIVE_PARENT_CHANGED");
        return fact;
      });
      let rejected = 0;
      for (const fact of facts) {
        try {
          await options.admit(fact);
        } catch (e) {
          // Only immutable/invalid source data is permanently rejected. Storage,
          // quota, checkpoint and unknown failures must be retried by OTLP senders.
          // Previously committed spans in this batch deduplicate on resubmission.
          if (
            !(e instanceof AgentError) ||
            ![
              "OVERSIZE_SPAN",
              "INVALID_CURSOR",
              "HELD_IMMUTABLE_CONFLICT",
              "HELD_USAGE_PLAN",
              "HELD_USAGE_CONFLICT",
            ].includes(e.code)
          ) {
            res.writeHead(503, { "Retry-After": "1" }).end();
            return;
          }
          rejected++;
        }
      }
      const response =
        type === "application/json"
          ? JSON.stringify(
              rejected
                ? { partialSuccess: { rejectedSpans: String(rejected) } }
                : {},
            )
          : encodeResponse(rejected);
      res.writeHead(200, { "Content-Type": type }).end(response);
    } catch {
      if (!res.headersSent) res.writeHead(400).end();
    }
  });
  server.maxConnections = 8;
  server.requestTimeout = 3000;
  server.headersTimeout = 3000;
  server.timeout = 3000;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  return server;
}
