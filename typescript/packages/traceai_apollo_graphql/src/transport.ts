import type { Agent } from 'node:http';
import type { Socket } from 'node:net';
import type { ConnectionOptions } from 'node:tls';
import { ExportResultCode, type ExportResult } from '@opentelemetry/core';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace';

type ExporterOptions = NonNullable<ConstructorParameters<typeof OTLPTraceExporter>[0]>;
// Node's documented Agent extension point is missing from @types/node 22.
type OwnedAgent = Agent & {
  createConnection(options: ConnectionOptions, callback?: (error: Error | null, socket: Socket) => void): Socket;
};
const failure = (): ExportResult => ({
  code: ExportResultCode.FAILED, error: new Error('Apollo telemetry export failed'),
});

/**
 * Keep the maintained OTLP serializer/retry implementation, but own its agents.
 * Its socket inactivity timeout does not bound a collector that trickles bytes.
 * A batch-local agent lets the total deadline abort only that export, including
 * retries, without touching application sockets or other in-flight batches.
 */
export class DeadlineOTLPExporter implements SpanExporter {
  private readonly pending = new Set<{ done: Promise<void>; cancel: () => void }>();
  private stopped = false;
  private shutdownPromise?: Promise<void>;
  constructor(private readonly options: ExporterOptions, private readonly timeoutMillis: number) {}

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    if (this.stopped) { callback(failure()); return; }
    const agents = new Set<Agent>();
    let closed = false;
    const inner = new OTLPTraceExporter({
      ...this.options, timeoutMillis: this.timeoutMillis,
      httpAgentOptions: protocol => {
        if (closed) throw new Error('Apollo telemetry export closed');
        // Load lazily, after HTTP instrumentation has installed its hooks.
        const AgentClass: typeof Agent = require(protocol === 'http:' ? 'node:http' : 'node:https').Agent;
        const agent = new AgentClass({ keepAlive: false }) as OwnedAgent;
        const connect = agent.createConnection.bind(agent);
        agent.createConnection = (options, callback) => {
          // Agent.destroy() alone does not prevent a scheduled retry from opening
          // another socket. Reject new connections after this batch is closed.
          if (closed) throw new Error('Apollo telemetry export closed');
          return connect(options, callback);
        };
        agents.add(agent);
        return agent;
      },
    });
    let resolve!: () => void;
    const done = new Promise<void>(finish => { resolve = finish; });
    const batch = { done, cancel: () => complete(failure()) };
    const timer = setTimeout(batch.cancel, this.timeoutMillis);
    const complete = (result: ExportResult): void => {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      for (const agent of agents) agent.destroy();
      // Always drain the maintained exporter's promise queue, even when the
      // processor's flush rejects. Do not await from inside its own callback.
      void Promise.resolve().then(() => inner.shutdown()).catch(() => {}).finally(() => {
        this.pending.delete(batch);
        resolve();
      });
      callback(result);
    };
    this.pending.add(batch);
    try { inner.export(spans, complete); }
    catch { complete(failure()); }
  }

  async forceFlush(): Promise<void> {
    await Promise.all([...this.pending].map(batch => batch.done));
  }

  shutdown(): Promise<void> {
    if (!this.shutdownPromise) {
      this.stopped = true;
      for (const batch of this.pending) batch.cancel();
      this.shutdownPromise = this.forceFlush();
    }
    return this.shutdownPromise;
  }
}
