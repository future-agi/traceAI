/**
 * Pi spans exported by the real fi-core register() exporter to a loopback
 * collector. The session id reaches the collector as set, under session.id and
 * pi.session.id, on every span and on later sessions with the same id. A
 * session id equal to a configured FI key is still scrubbed.
 *
 * Loopback only; placeholder keys; no model call.
 */
import { createServer, Server } from "http";
import { AddressInfo } from "net";
import { ProjectType, register } from "@traceai/fi-core";
import { Attr, PiCodingAgentInstrumentation, SpanNames } from "../index";
import { FakeSession, SESSION_ID, happyPath } from "./fixtures/events";

const API_KEY = "pi-export-api-key-PLACEHOLDER";
const SECRET_KEY = "pi-export-secret-key-PLACEHOLDER";

type OtlpValue = { stringValue?: string; intValue?: string | number; doubleValue?: number; boolValue?: boolean };
type OtlpSpan = { name: string; attributes?: { key: string; value: OtlpValue }[] };
type OtlpBody = { resourceSpans: { scopeSpans: { spans: OtlpSpan[] }[] }[] };

function attributes(span: OtlpSpan): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const { key, value } of span.attributes ?? []) {
    out[key] = value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue;
  }
  return out;
}

describe("Pi session id through the FI exporter", () => {
  let server: Server;
  let origin: string;
  const bodies: string[] = [];
  const saved = { ...process.env };

  beforeAll(async () => {
    // No privacy flag or endpoint may come from the environment.
    for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
    process.env.FI_API_KEY = API_KEY;
    process.env.FI_SECRET_KEY = SECRET_KEY;
    server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        bodies.push(Buffer.concat(chunks).toString("utf8"));
        res.writeHead(200, { "content-type": "application/json" });
        res.end("{}");
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterAll(async () => {
    process.env = { ...saved };
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  /** Run one Pi session through register()'s exporter; return what the collector received. */
  async function exportSession(sessionId: string): Promise<{ spans: OtlpSpan[]; text: string }> {
    bodies.length = 0;
    const provider = register({
      projectName: "pi-session-export",
      projectType: ProjectType.OBSERVE,
      endpoint: origin,
      batch: true,
      setGlobalTracerProvider: false,
    });
    try {
      const session = new FakeSession(sessionId);
      const stop = new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
      session.emit(happyPath());
      stop();
      await provider.forceFlush();
    } finally {
      await provider.shutdown();
    }
    const spans: OtlpSpan[] = [];
    for (const body of bodies) {
      for (const resource of (JSON.parse(body) as OtlpBody).resourceSpans) {
        for (const scope of resource.scopeSpans) spans.push(...scope.spans);
      }
    }
    return { spans, text: bodies.join("\n") };
  }

  it("exports the session id as set on every span, under session.id and pi.session.id", async () => {
    const { spans } = await exportSession(SESSION_ID);
    expect(spans.map((span) => span.name)).toContain(SpanNames.AGENT_RUN);
    expect(spans.length).toBeGreaterThan(1);
    for (const span of spans) {
      const a = attributes(span);
      expect([span.name, a[Attr.SESSION_ID], a[Attr.PI_SESSION_ID]]).toEqual([span.name, SESSION_ID, SESSION_ID]);
    }
    const run = spans.find((span) => span.name === SpanNames.AGENT_RUN)!;
    expect(attributes(run)[Attr.GEN_AI_CONVERSATION_ID]).toBe(SESSION_ID);
  });

  it("keeps the id as set on a later session with the same id", async () => {
    await exportSession(SESSION_ID);
    const { spans } = await exportSession(SESSION_ID);
    expect(spans.length).toBeGreaterThan(1);
    for (const span of spans) {
      const a = attributes(span);
      expect([span.name, a[Attr.SESSION_ID], a[Attr.PI_SESSION_ID]]).toEqual([span.name, SESSION_ID, SESSION_ID]);
    }
  });

  it("still scrubs a session id equal to the configured FI secret key", async () => {
    const { spans, text } = await exportSession(SECRET_KEY);
    expect(spans.length).toBeGreaterThan(1);
    for (const span of spans) {
      const a = attributes(span);
      expect([span.name, a[Attr.SESSION_ID], a[Attr.PI_SESSION_ID]]).toEqual([span.name, "<redacted>", "<redacted>"]);
    }
    expect(text).not.toContain(SECRET_KEY);
    expect(text).not.toContain(API_KEY);
  });
});
