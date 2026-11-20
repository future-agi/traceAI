import { InMemorySpanExporter, ReadableSpan, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { createServer, IncomingMessage, Server } from "http";
import { AddressInfo } from "net";
import { SageMakerInstrumentation } from "../instrumentation";

/**
 * Offline harness. The real SDK client runs; its network step is replaced by a canned answer at the
 * start of the middleware stack, or by a loopback HTTP server. Nothing leaves the process.
 */
export const SDKS = ["@aws-sdk/client-sagemaker-runtime", "@aws-sdk/client-sagemaker-runtime-floor"] as const;
export type SdkName = (typeof SDKS)[number];
export const ENDPOINT = "llm-endpoint-1";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Sdk = any;

export function sdk(name: SdkName): Sdk {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require(name);
}

export function tracing() {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  return { exporter, provider, spans: (): ReadableSpan[] => exporter.getFinishedSpans() };
}

export function instrument(name: SdkName, traceConfig: Record<string, boolean> = {}) {
  const t = tracing();
  const lib = sdk(name);
  const instrumentation = new SageMakerInstrumentation({ traceConfig });
  instrumentation.setTracerProvider(t.provider);
  instrumentation.manuallyInstrument(lib);
  return { ...t, lib, instrumentation };
}

export function client(lib: Sdk, reply?: (command: unknown) => unknown | Promise<unknown>) {
  const c = new lib.SageMakerRuntimeClient({
    region: "us-east-1",
    credentials: { accessKeyId: "testing", secretAccessKey: "testing" },
  });
  if (reply) {
    c.middlewareStack.add(
      () => async (args: { input: unknown }) => ({ output: await reply(args), response: {} }),
      { step: "initialize", name: "stub", priority: "low" },
    );
  }
  return c;
}

export const bytes = (value: unknown) =>
  typeof value === "string" ? new TextEncoder().encode(value) : new TextEncoder().encode(JSON.stringify(value));

export async function* events(parts: Array<string | Uint8Array>, error?: Error) {
  for (const part of parts) {
    yield { PayloadPart: { Bytes: typeof part === "string" ? bytes(part) : part } };
  }
  if (error) throw error;
}

export interface Loopback { url: string; close(): Promise<void>; seen: Array<{ url: string; body: string; headers: IncomingMessage["headers"] }> }

export async function loopback(
  respond: (body: string) => { status?: number; headers?: Record<string, string>; body: string | Uint8Array },
): Promise<Loopback> {
  const seen: Loopback["seen"] = [];
  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf8");
      seen.push({ url: req.url ?? "", body, headers: req.headers });
      const reply = respond(body);
      res.writeHead(reply.status ?? 200, reply.headers ?? {});
      res.end(reply.body);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  return {
    url: `http://127.0.0.1:${port}`,
    seen,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

export const dumpOf = (spans: ReadableSpan[]) =>
  JSON.stringify(spans.map((s) => ({ n: s.name, a: s.attributes, e: s.events, s: s.status, l: s.links })));
