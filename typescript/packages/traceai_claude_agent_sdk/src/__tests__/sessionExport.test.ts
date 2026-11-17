/**
 * Claude Agent SDK spans exported by the real fi-core register() exporter to a
 * loopback collector. The session id reaches the collector as set, under
 * session.id and the package's own session keys, including the resume and fork
 * ids, and later queries with the same id keep it. A session id equal to a
 * configured FI key is still scrubbed.
 *
 * Loopback only; placeholder keys; no Anthropic call.
 */
import { createServer, Server } from "http";
import { AddressInfo } from "net";
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { ProjectType, register } from "@traceai/fi-core";
import { shutdown, wrapQuery } from "../index";
import { makeFakeQuery } from "./fixtures/fakeQuery";
import { PROMPT, SESSION_ID, assistant, init, resultSuccess, simpleToolJourney, text } from "./fixtures/messages";
import { drain } from "./helpers";

const API_KEY = "claude-export-api-key-PLACEHOLDER";
const SECRET_KEY = "claude-export-secret-key-PLACEHOLDER";
const RESUMED = "aaaaaaaa-bbbb-4ccc-8ddd-000000000a01";
const PARENT = "aaaaaaaa-bbbb-4ccc-8ddd-000000000b01";
const FORK = "aaaaaaaa-bbbb-4ccc-8ddd-000000000b02";

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

/** One answer from a session with the given id. */
function journey(sessionId: string): SDKMessage[] {
  return [
    init({ session_id: sessionId }),
    assistant("msg_session", [text("Answer.")], null, { session_id: sessionId }),
    resultSuccess("Answer.", { session_id: sessionId }),
  ];
}

describe("Claude Agent SDK session id through the FI exporter", () => {
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

  /** Run one wrapped query through register()'s exporter; return what the collector received. */
  async function exportQuery(
    messages: SDKMessage[],
    options: Record<string, unknown> = {},
  ): Promise<{ spans: OtlpSpan[]; conversation: Record<string, unknown>; text: string }> {
    bodies.length = 0;
    const provider = register({
      projectName: "claude-session-export",
      projectType: ProjectType.OBSERVE,
      endpoint: origin,
      batch: true,
      setGlobalTracerProvider: false,
    });
    const traced = wrapQuery(makeFakeQuery(messages).query, { tracerProvider: provider });
    await drain(traced({ prompt: PROMPT, options }));
    await shutdown(provider);
    await provider.shutdown();
    const spans: OtlpSpan[] = [];
    for (const body of bodies) {
      for (const resource of (JSON.parse(body) as OtlpBody).resourceSpans) {
        for (const scope of resource.scopeSpans) spans.push(...scope.spans);
      }
    }
    const conversation = spans.find((span) => span.name === "claude_agent.conversation");
    expect(conversation).toBeDefined();
    return { spans, conversation: attributes(conversation!), text: bodies.join("\n") };
  }

  function expectSessionOnEverySpan(spans: OtlpSpan[], expected: string): void {
    expect(spans.length).toBeGreaterThan(1);
    for (const span of spans) {
      const a = attributes(span);
      expect([span.name, a["session.id"], a["claude_agent.session.id"], a["claude_agent.session_id"]]).toEqual([
        span.name,
        expected,
        expected,
        expected,
      ]);
    }
  }

  it("exports the session id as set on every span, under session.id and both package keys", async () => {
    const { spans } = await exportQuery(simpleToolJourney());
    expect(spans.map((span) => span.name)).toContain("tool.Read");
    expectSessionOnEverySpan(spans, SESSION_ID);
  });

  it("keeps the id as set on a resumed query, also as the resume and previous session ids", async () => {
    await exportQuery(journey(RESUMED));
    const { spans, conversation } = await exportQuery(journey(RESUMED), { resume: RESUMED });
    expectSessionOnEverySpan(spans, RESUMED);
    expect(conversation["claude_agent.resume_session_id"]).toBe(RESUMED);
    expect(conversation["claude_agent.session.previous_id"]).toBe(RESUMED);
  });

  it("exports a fork's parent id as set, and later queries in the parent session keep it", async () => {
    const fork = await exportQuery(journey(FORK), { resume: PARENT, forkSession: true });
    expectSessionOnEverySpan(fork.spans, FORK);
    expect(fork.conversation["claude_agent.session.fork_from"]).toBe(PARENT);
    expect(fork.conversation["claude_agent.resume_session_id"]).toBe(PARENT);
    const parent = await exportQuery(journey(PARENT), { resume: PARENT });
    expectSessionOnEverySpan(parent.spans, PARENT);
  });

  it("still scrubs a session id equal to the configured FI secret key", async () => {
    const { spans, text } = await exportQuery(journey(SECRET_KEY));
    expectSessionOnEverySpan(spans, "<redacted>");
    expect(text).not.toContain(SECRET_KEY);
    expect(text).not.toContain(API_KEY);
  });
});
