import { InMemorySpanExporter, ReadableSpan, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

/** Offline harness: the real SDK talks to a fake `fetch`; nothing leaves the process. */
export const SDKS = ["@typesafe-ai/sdk", "@typesafe-ai/sdk-floor"] as const;
export const SECRET_KEY = "synthetic-api-key-do-not-use";
export const BASE_URL = "http://typesafe.invalid";

export const WIRE = {
  model: "jev-2026-10-01",
  answers: {
    billing: { type: "noul", noul: 0.93 },
    tone: { type: "choice", choice: "angry", confidence: 0.8, probabilities: { calm: 0.2, angry: 0.8 } },
    quality: {
      type: "score", score: 2.4, confidence: 0.7,
      legend: { "0": "legend-zero", "1": "legend-one", "2": "legend-two" }, probabilities: { "0": 0.1, "1": 0.2, "2": 0.7 },
    },
    where: { type: "box", box: { top: 0.1, bottom: 0.5, left: 0.2, right: 0.6 }, confidence: null },
  },
  usage: { input_tokens: 12, output_tokens: 3 },
};

export interface Sent { url: string; init: RequestInit }
export interface Reply { status?: number; body?: unknown; headers?: Record<string, string> }

export function fakeFetch(replies: Reply[] | ((n: number) => Reply)) {
  const sent: Sent[] = [];
  const fetch = async (url: string, init?: RequestInit) => {
    const reply = typeof replies === "function" ? replies(sent.length) : replies[Math.min(sent.length, replies.length - 1)];
    sent.push({ url, init: init ?? {} });
    return new Response(JSON.stringify(reply.body ?? WIRE), {
      status: reply.status ?? 200,
      headers: { "content-type": "application/json", "x-typesafe-request-id": "req-synthetic-1", ...reply.headers },
    });
  };
  return { fetch, sent };
}

export function tracing() {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  return { exporter, provider, spans: (): ReadableSpan[] => exporter.getFinishedSpans() };
}

export function sdk(name: (typeof SDKS)[number]) {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require(name) as typeof import("@typesafe-ai/sdk");
}
