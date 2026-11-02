import { createHash } from "node:crypto";
import {
  Attributes,
  context,
  Context,
  createContextKey,
  diag,
  Span,
  SpanKind,
  SpanStatusCode,
  trace,
  Tracer,
} from "@opentelemetry/api";

/** Explicit, version-qualified npm fastmcp operation tracing. */
export interface FastMCPOptions {
  /** Installed npm fastmcp version; prereleases and versions outside 3.35.x/4.x are rejected. */
  version: string;
  /** Off by default. Error details, metadata, credentials and binary blocks are never captured. */
  captureContent?: { input?: boolean; output?: boolean; maxLength?: number };
  /** Optional end-user conversation ID, not the transport/session/request ID. */
  sessionId?: string;
  /** Bound incomplete operations without cancelling application work. Default: 5 minutes. */
  maxDurationMs?: number;
  /** Maximum in-flight spans per transport. Default: 1024. Excess requests still execute. */
  maxPendingRequests?: number;
}

type RecordValue = Record<string, unknown>;
export const isRecord = (value: unknown): value is RecordValue =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const requestKey = createContextKey("traceai.mcp.operation");
const methods = new Set([
  "initialize",
  "ping",
  "tools/call",
  "tools/list",
  "resources/read",
  "resources/list",
  "resources/templates/list",
  "prompts/get",
  "prompts/list",
  "resources/subscribe",
  "resources/unsubscribe",
  "completion/complete",
  "logging/setLevel",
]);
const contentMethods = new Set(["tools/call", "resources/read", "prompts/get"]);
const bounded = (value: number | undefined, fallback: number, max: number) =>
  Number.isFinite(value) && value! > 0
    ? Math.min(Math.floor(value!), max)
    : fallback;

// Only plain JSON data is visited, with a work budget as well as a byte budget.
// No toJSON/getter execution, arbitrary _meta, transport/auth data, or binary blocks.
function content(value: unknown, limit: number): string | undefined {
  let budget = limit;
  let nodes = 256;
  const seen = new WeakSet<object>();
  const visit = (item: unknown, depth: number): unknown => {
    if (--nodes < 0 || budget <= 0 || depth > 6) return undefined;
    if (typeof item === "string") {
      const text = item.slice(0, budget);
      budget -= text.length;
      return text;
    }
    if (item === null || typeof item === "boolean" || typeof item === "number")
      return item;
    if (typeof item !== "object" || seen.has(item)) return undefined;
    seen.add(item);
    if (Array.isArray(item)) {
      const result: unknown[] = [];
      for (let index = 0; index < Math.min(item.length, 64); index++) {
        const descriptor = Object.getOwnPropertyDescriptor(item, String(index));
        result.push(
          descriptor && "value" in descriptor
            ? visit(descriptor.value, depth + 1)
            : null,
        );
      }
      return result;
    }
    if (
      Object.getPrototypeOf(item) !== Object.prototype &&
      Object.getPrototypeOf(item) !== null
    )
      return undefined;
    const type = Object.getOwnPropertyDescriptor(item, "type")?.value;
    if (["image", "audio", "resource_link"].includes(type)) return undefined;
    const result: RecordValue = {};
    let keys = 0;
    for (const key in item) {
      if (++keys > 64 || budget <= 0 || nodes <= 0) break;
      if (
        /(?:^_meta$|auth|bearer|cookie|credential|password|passphrase|secret|token|key|cert|signature|^headers$|^data$|^blob$|^uri$|^url$|^stack$)/i.test(
          key,
        )
      )
        continue;
      const descriptor = Object.getOwnPropertyDescriptor(item, key);
      if (!descriptor || !("value" in descriptor)) continue;
      budget -= key.length;
      const child = visit(descriptor.value, depth + 1);
      if (child !== undefined)
        Object.defineProperty(result, key, { value: child, enumerable: true });
    }
    return result;
  };
  try {
    const json = JSON.stringify(visit(value, 0));
    // Keep valid JSON: oversized content is omitted, not truncated mid-string.
    return json !== undefined && Buffer.byteLength(json) <= limit
      ? json
      : undefined;
  } catch {
    // Do not put the rejected content or a serialization exception in diagnostics.
    diag.debug("MCP content capture could not serialize this value");
    return undefined;
  }
}
function hidden(direction: "INPUT" | "OUTPUT"): boolean {
  return ["S", "_TEXT", "_MESSAGES"].some(
    (suffix) =>
      process.env[`FI_HIDE_${direction}${suffix}`]?.trim().toLowerCase() ===
      "true",
  );
}

interface Pending {
  span: Span;
  context: Context;
  timer: ReturnType<typeof setTimeout>;
  method: string;
}

/** Internal hooks used by the existing propagation adapter, never SDK-private handler maps. */
export class Operations {
  private readonly pending = new Map<string | number, Pending>();
  private readonly maxPending: number;
  private readonly maxDuration: number;
  private readonly limit: number;
  constructor(
    private readonly tracer: Tracer,
    private readonly options: FastMCPOptions,
    private readonly transportSessionId: () => unknown,
    private readonly onClose: () => void = () => {},
  ) {
    this.maxPending = bounded(options.maxPendingRequests, 1024, 10000);
    this.maxDuration = bounded(options.maxDurationMs, 300000, 3600000);
    this.limit = bounded(options.captureContent?.maxLength, 4096, 16384);
  }

  receive(message: unknown, dispatch: () => unknown): unknown {
    if (!isRecord(message)) return dispatch();
    if (
      message.method === "notifications/cancelled" &&
      isRecord(message.params)
    ) {
      this.finish(message.params.requestId, "cancelled");
      return dispatch();
    }
    const id = message.id;
    if (
      typeof message.method !== "string" ||
      (typeof id !== "string" && typeof id !== "number")
    )
      return dispatch();
    const existing = this.pending.get(id);
    // A transport can be reached through both its public adapter and prototype.
    if (existing && context.active().getValue(requestKey) === existing) {
      return context.with(existing.context, dispatch);
    }
    // Invalid duplicate IDs must not leak spans or replace another request's state.
    if (existing || this.pending.size >= this.maxPending) return dispatch();
    const method = methods.has(message.method) ? message.method : "unknown";
    const params = isRecord(message.params) ? message.params : {};
    const attributes: Attributes = {
      "rpc.system": "jsonrpc",
      "rpc.method": method,
      "mcp.request.id": typeof id === "string" ? id.slice(0, 256) : id,
      "fi.span.kind": method === "tools/call" ? "TOOL" : "CHAIN",
    };
    if (
      typeof params.name === "string" &&
      ["tools/call", "prompts/get"].includes(method)
    ) {
      attributes[method === "tools/call" ? "tool.name" : "mcp.prompt.name"] =
        params.name.slice(0, 256);
    }
    if (method === "resources/read" && typeof params.uri === "string") {
      // URIs commonly embed PII or signed query parameters. Identify, don't disclose.
      attributes["mcp.resource.uri_hash"] = createHash("sha256")
        .update(params.uri)
        .digest("hex");
    }
    const session = this.transportSessionId();
    if (typeof session === "string" && session)
      attributes["mcp.session.id"] = session.slice(0, 256);
    if (this.options.sessionId)
      attributes["session.id"] = this.options.sessionId.slice(0, 256);
    const span = this.tracer.startSpan(`fastmcp ${method}`, {
      kind: SpanKind.SERVER,
      attributes,
    });
    if (
      contentMethods.has(method) &&
      this.options.captureContent?.input &&
      !hidden("INPUT")
    ) {
      const input = content(params.arguments, this.limit);
      if (input !== undefined)
        span.setAttributes({
          "input.value": input,
          "input.mime_type": "application/json",
        });
    }
    const pending: Pending = {
      span,
      method,
      context: context.active(),
      timer: setTimeout(
        () => this.finish(id, "deadline_exceeded"),
        this.maxDuration,
      ),
    };
    pending.timer.unref?.();
    pending.context = trace
      .setSpan(context.active(), span)
      .setValue(requestKey, pending);
    this.pending.set(id, pending);
    try {
      // MCP dispatch schedules async work but does not return its completion promise.
      // End only when its response is sent (or cancellation/close/deadline occurs).
      const result = context.with(pending.context, dispatch);
      // Some custom transports return a dispatch promise; observe rejection only.
      if (result instanceof Promise)
        void result.then(undefined, () => {
          if (this.pending.get(id) === pending)
            this.finish(id, "dispatch_error");
        });
      return result;
    } catch (error) {
      this.finish(id, "dispatch_error");
      throw error;
    }
  }

  send(message: unknown, send: () => unknown): unknown {
    if (
      !isRecord(message) ||
      "method" in message ||
      !("result" in message || "error" in message)
    )
      return send();
    const id = message.id;
    const pending =
      typeof id === "number" || typeof id === "string"
        ? this.pending.get(id)
        : undefined;
    if (!pending) return send();
    const error =
      "error" in message ||
      (isRecord(message.result) && message.result.isError === true);
    // Keep the original return value/promise, receiver and transport options.
    try {
      const result = send();
      void Promise.resolve(result).then(
        () => {
          if (this.pending.get(id as string | number) !== pending) return;
          if (
            !error &&
            contentMethods.has(pending.method) &&
            this.options.captureContent?.output &&
            !hidden("OUTPUT")
          ) {
            const output = content(message.result, this.limit);
            if (output !== undefined)
              pending.span.setAttributes({
                "output.value": output,
                "output.mime_type": "application/json",
              });
          }
          this.finish(id, error ? "operation_error" : undefined);
        },
        () => {
          if (this.pending.get(id as string | number) === pending)
            this.finish(id, "send_error");
        },
      );
      return result;
    } catch (error) {
      if (this.pending.get(id as string | number) === pending)
        this.finish(id, "send_error");
      throw error;
    }
  }

  close(): void {
    for (const id of this.pending.keys()) this.finish(id, "closed");
    this.onClose();
  }

  private finish(id: unknown, reason?: string): void {
    if (typeof id !== "string" && typeof id !== "number") return;
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    clearTimeout(pending.timer);
    if (reason) {
      pending.span.setStatus({ code: SpanStatusCode.ERROR });
      pending.span.setAttribute("error.type", reason);
    } else pending.span.setStatus({ code: SpanStatusCode.OK });
    pending.span.end();
  }
}

export { transportOperations } from "./state";
