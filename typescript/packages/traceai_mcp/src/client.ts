import { context, Span, SpanKind, SpanStatusCode, trace, TracerProvider } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentCapturePolicy, AttachmentManager } from "@traceai/fi-core/attachments";
import { captureMCPResult } from "./attachments";
import { VERSION } from "./version";

export interface MCPClientTracingConfig {
  tracerProvider?: TracerProvider;
  /** The SAME manager as the provider/auto runtime; never creates another uploader. */
  attachments?: AttachmentManager;
  traceConfig?: TraceConfigOptions;
  attachmentCapture?: AttachmentCapturePolicy;
}

const WRAPPED = new WeakSet<object>();

/**
 * Trace the actual client callTool/readResource boundaries (not transport refs).
 * The proxy preserves SDK receiver, argument/result/error identity and cancellation.
 * Only successfully returned typed bytes are observed. Network work is never awaited.
 */
export function wrapMCPClient<C extends object>(client: C, config: MCPClientTracingConfig = {}): C {
  if (WRAPPED.has(client)) return client;
  const methods = new Map<PropertyKey, unknown>();
  const proxy = new Proxy(client, {
    get(target, key) {
      if (methods.has(key)) return methods.get(key);
      const value = Reflect.get(target, key, target);
      if (typeof value !== "function") return value;
      if (key !== "callTool" && key !== "readResource") return value.bind(target);
      const operation = key === "callTool" ? "tools/call" : "resources/read";
      const wrapped = (...args: unknown[]) => {
        let span: Span | undefined;
        let attachments: AttachmentManager | undefined;
        try {
          const provider = config.tracerProvider ?? trace.getTracerProvider();
          const owner = provider as TracerProvider & { attachments?: AttachmentManager; getDelegate?: () => TracerProvider & { attachments?: AttachmentManager } };
          attachments = config.attachments ?? owner.attachments ?? owner.getDelegate?.().attachments;
          const tracer = new FITracer({ tracer: provider.getTracer("@traceai/mcp", VERSION), traceConfig: config.traceConfig ?? {} });
          span = tracer.startSpan(`mcp.${operation}`, { kind: SpanKind.CLIENT, attributes: { "gen_ai.span.kind": "TOOL", "mcp.method.name": operation } });
        } catch { /* Tracing cannot prevent the SDK call. */ }
        const finish = (failed: boolean, result?: unknown) => {
          try {
            if (!span) return;
            if (!failed && attachments) captureMCPResult(attachments, span, result, operation, config.attachmentCapture, config.traceConfig);
            const toolError = result && typeof result === "object" && (result as { isError?: unknown }).isError === true;
            span.setStatus({ code: failed || toolError ? SpanStatusCode.ERROR : SpanStatusCode.OK });
          } catch { /* Never log raw SDK exceptions. */ }
          finally { try { span?.end(); } catch { /* Best effort. */ } }
        };
        let result;
        try {
          result = span
            ? context.with(trace.setSpan(context.active(), span), () => value.apply(target, args))
            : value.apply(target, args);
        } catch (error) { finish(true); throw error; }
        // Observe a side branch, but return the exact SDK promise to the caller.
        try { result.then((response: unknown) => finish(false, response), () => finish(true)); }
        catch { finish(true); }
        return result;
      };
      methods.set(key, wrapped);
      return wrapped;
    },
  });
  WRAPPED.add(proxy);
  return proxy;
}
