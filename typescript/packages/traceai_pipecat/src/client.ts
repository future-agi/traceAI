import {
  context,
  Context,
  diag,
  SpanKind,
  SpanStatusCode,
  trace,
  Tracer,
} from "@opentelemetry/api";
import { VERSION } from "./version";

const EVENTS = [
  "connected", "disconnected", "botReady", "transportStateChanged",
  "error", "messageError",
] as const;

type ClientEvent = typeof EVENTS[number];

/** The public event interface shared by RTVIClient and PipecatClient. */
export interface PipecatEventSource {
  on(event: ClientEvent, listener: (...args: unknown[]) => void): unknown;
  off(event: ClientEvent, listener: (...args: unknown[]) => void): unknown;
}

export interface PipecatClientTracingOptions {
  tracer?: Tracer;
  /** Optional application-owned parent; never sent to the bot or transport. */
  parentContext?: Context;
}

const observers = new WeakMap<PipecatEventSource, () => void>();

/**
 * Observe client lifecycle events without patching the SDK or replacing callbacks.
 * Payloads, transcripts, connection parameters and error details are never captured.
 * Call the returned function when disposing the client. Repeated attachment is a no-op.
 */
export function observePipecatClient(
  client: PipecatEventSource,
  options: PipecatClientTracingOptions = {},
): () => void {
  const existing = observers.get(client);
  if (existing) return existing;

  const tracer = options.tracer ?? trace.getTracer("@traceai/fi-instrumentation-pipecat", VERSION);
  const listeners = EVENTS.map((event) => {
    const listener = () => {
      // Telemetry must not interrupt application callbacks. Do not log their data.
      try {
        const span = tracer.startSpan(`Pipecat Client Event: ${event}`, {
          kind: SpanKind.CLIENT,
          attributes: {
            "fi.span.kind": "CHAIN",
            "pipecat.telemetry_source": "client",
            "pipecat.event": event,
          },
        }, options.parentContext ?? context.active());
        span.setStatus({ code: event === "error" || event === "messageError" ? SpanStatusCode.ERROR : SpanStatusCode.OK });
        span.end();
      } catch {
        diag.warn("Pipecat client event telemetry failed");
      }
    };
    return { event, listener };
  });

  const detach = () => {
    for (const { event, listener } of listeners) client.off(event, listener);
    if (observers.get(client) === detach) observers.delete(client);
  };
  try {
    for (const { event, listener } of listeners) client.on(event, listener);
  } catch (error) {
    detach();
    throw error;
  }
  observers.set(client, detach);
  return detach;
}
