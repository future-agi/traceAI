/**
 * The legacy whole-object hooks can attach to any SDK that exposes the patched prototypes, including
 * @google/adk 2.x (Agent.run is inherited from BaseNode there). Under the input hide settings no hook may
 * put request content into any attribute, and with no setting the recorded values are unchanged.
 */
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { GoogleADKInstrumentation, _resetPatchedStateForTesting } from "../instrumentation";

const REQ = "REQUEST-MARK-61a";
const ECHO = "ECHOED-REQUEST-MARK-72b";
const RESP = "RESPONSE-MARK-83c";
const INPUT_SETTINGS = ["hideInputs", "hideInputText", "hideInputMessages", "hideInputImages"];
// Image hiding leaves text messages to the text/message settings; the other three hide the request text.
const REQUEST_TEXT_SETTINGS = ["hideInputs", "hideInputText", "hideInputMessages"];
const ALL_SETTINGS = [...INPUT_SETTINGS, "hideOutputs", "hideOutputText", "hideOutputMessages"];

// Mirrors the prototype chain of @google/adk 2.5.0: run() lives on BaseNode and returns an async generator.
class BaseNode {
  name = "root";
  async *run(ctx: unknown, input: unknown): AsyncGenerator<unknown> {
    yield { author: this.name, content: { role: "model", parts: [{ text: RESP }] }, echoed: { ctx, input } };
  }
}
class Agent extends BaseNode {}

const legacyModule = () => ({
  Agent: {
    prototype: {
      run: async () => ({ response: { text: RESP, request: { message: ECHO } }, usage: { promptTokens: 1, completionTokens: 2 } }),
      invoke: async () => ({ text: RESP, prompt: ECHO, messages: [{ role: "user", content: ECHO }] }),
      stream: async () => (async function* () {
        yield { content: RESP, request: { message: ECHO } };
      })(),
    },
  },
  Tool: { prototype: { execute: async () => ({ text: RESP, arguments: { query: ECHO } }) } },
  Runner: { prototype: { run: async () => ({ output: { text: RESP, input: { message: ECHO } } }) } },
  Session: { prototype: { sendMessage: async () => ({ content: RESP, request: ECHO }) } },
});

async function exercise(adk: any, traceConfig: Record<string, boolean>) {
  _resetPatchedStateForTesting();
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const inst = new GoogleADKInstrumentation({ traceConfig });
  inst.setTracerProvider(provider);
  inst.enable();
  inst.manuallyInstrument(adk);
  const agent = new adk.Agent();
  agent.name = "agent";
  const request = { message: REQ };
  if (adk.Agent.prototype.run) {
    const out = await agent.run(request, request);
    if (out && typeof out[Symbol.asyncIterator] === "function") for await (const _ of out) { /* drain */ }
  }
  if (adk.Agent.prototype.invoke) await agent.invoke(request);
  if (adk.Agent.prototype.stream) for await (const _ of await agent.stream(request)) { /* drain */ }
  if (adk.Tool) await new adk.Tool().execute({ query: REQ });
  if (adk.Runner) await new adk.Runner().run(request);
  if (adk.Session) await new adk.Session().sendMessage(REQ);
  inst.disable();
  const spans = exporter.getFinishedSpans();
  await provider.shutdown();
  return { spans, dump: JSON.stringify(spans.map((s) => ({ n: s.name, a: s.attributes, e: s.events }))) };
}

const adk25 = () => {
  const Agent25 = Agent;
  return { Agent: Agent25 };
};
// A shape-specific class double needs its own constructor for the prototype-wrapping helper.
function legacyWithClasses() {
  const m = legacyModule();
  const mk = (proto: object) => { const C = function () { /* instance */ } as any; C.prototype = proto; return C; };
  return { Agent: mk(m.Agent.prototype), Tool: mk(m.Tool.prototype), Runner: mk(m.Runner.prototype), Session: mk(m.Session.prototype) };
}

describe("legacy hooks on an SDK whose Agent.run is inherited (2.x shape)", () => {
  it("records the run with no setting (control)", async () => {
    const { spans, dump } = await exercise(adk25(), {});
    expect(spans.length).toBeGreaterThan(0);
    expect(dump).toContain(REQ);
  });

  it.each(ALL_SETTINGS)("%s keeps request content out of every attribute", async (setting) => {
    const { spans, dump } = await exercise(adk25(), { [setting]: true });
    expect(spans.length).toBeGreaterThan(0);
    if (REQUEST_TEXT_SETTINGS.includes(setting)) expect(dump).not.toContain(REQ);
    for (const span of spans) {
      expect(span.attributes["raw.output"]).toBeUndefined();
      expect(span.attributes["raw.input"]).toBeUndefined();
    }
    expect(dump).not.toContain(ECHO);
  });
});

describe("legacy hooks on result objects that echo the request", () => {
  it("records response text and the whole result with no setting (control)", async () => {
    const { dump } = await exercise(legacyWithClasses(), {});
    expect(dump).toContain(RESP);
    expect(dump).toContain(ECHO);
  });

  it.each(INPUT_SETTINGS)("%s: output values come from response-side fields only", async (setting) => {
    const { spans, dump } = await exercise(legacyWithClasses(), { [setting]: true });
    if (REQUEST_TEXT_SETTINGS.includes(setting)) expect(dump).not.toContain(REQ);
    expect(dump).not.toContain(ECHO);
    expect(dump).toContain(RESP);
    for (const span of spans) expect(span.attributes["raw.output"]).toBeUndefined();
    for (const span of spans) expect(span.attributes["raw.input"]).toBeUndefined();
  });

  it.each(["hideOutputs", "hideOutputText", "hideOutputMessages"])("%s keeps the response out and writes no raw copy", async (setting) => {
    const { spans, dump } = await exercise(legacyWithClasses(), { [setting]: true });
    expect(dump).not.toContain(RESP);
    for (const span of spans) expect(span.attributes["raw.output"]).toBeUndefined();
  });
});
