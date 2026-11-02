import { CerebrasInstrumentation, isPatched } from "../instrumentation";
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";

const body = {
  model: "test-model",
  messages: [{ role: "user", content: "hello" }],
  temperature: 0.5,
};
const reply = {
  model: "test-model",
  choices: [
    {
      index: 0,
      message: { role: "assistant", content: "world" },
      finish_reason: "stop",
    },
  ],
};
function moduleWith(create = jest.fn().mockResolvedValue(reply)) {
  class Completions {
    create = create;
  }
  // The real SDK's method is on the resource prototype, not the client prototype.
  Completions.prototype.create = create;
  return { Cerebras: { Chat: { Completions } } };
}

describe("Cerebras instrumentation lifecycle and edge cases", () => {
  let instrumentation: CerebrasInstrumentation;
  let exporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    instrumentation = new CerebrasInstrumentation();
    instrumentation.setTracerProvider(provider);
  });
  afterEach(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it("leaves unsupported exports untouched and can still patch a valid module", () => {
    const invalid = Object.freeze({ default: class {} });
    instrumentation.manuallyInstrument(invalid);
    expect(isPatched()).toBe(false);
    const valid = Object.freeze(moduleWith());
    instrumentation.manuallyInstrument(valid);
    expect(isPatched()).toBe(true);
  });

  it("patches independent SDK resources without a process-wide skip flag", async () => {
    const a = moduleWith();
    const b = moduleWith();
    instrumentation.manuallyInstrument(a);
    instrumentation.manuallyInstrument(b);
    await a.Cerebras.Chat.Completions.prototype.create(body);
    await b.Cerebras.Chat.Completions.prototype.create(body);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("does not double-wrap or unpatch another instrumentation's resources", async () => {
    const module = moduleWith();
    instrumentation.manuallyInstrument(module);
    const other = new CerebrasInstrumentation();
    other.manuallyInstrument(module);
    other.disable();
    await module.Cerebras.Chat.Completions.prototype.create(body);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(isPatched()).toBe(true);
  });

  it("uses a provider set after manual instrumentation", async () => {
    const module = moduleWith();
    instrumentation.manuallyInstrument(module);
    instrumentation.setTracerProvider(provider);
    await module.Cerebras.Chat.Completions.prototype.create(body);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("captures input, output and invocation parameters through the public entry point", async () => {
    const module = moduleWith();
    instrumentation.manuallyInstrument(module);
    await module.Cerebras.Chat.Completions.prototype.create(body);
    const attrs = exporter.getFinishedSpans()[0].attributes;
    expect(JSON.parse(attrs[S.LLM_INPUT_MESSAGES] as string)).toEqual(
      body.messages,
    );
    expect(JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES] as string)).toEqual([
      reply.choices[0].message,
    ]);
    expect(JSON.parse(attrs[S.LLM_INVOCATION_PARAMETERS] as string)).toEqual({
      model: "test-model",
      temperature: 0.5,
    });
    expect(JSON.parse(attrs[S.RAW_INPUT] as string)).toEqual(body);
    expect(JSON.parse(attrs[S.RAW_OUTPUT] as string)).toEqual(reply);
  });

  it("preserves synchronous exceptions and ends the span", () => {
    const error = new Error("sync failure");
    const module = moduleWith(
      jest.fn(() => {
        throw error;
      }),
    );
    instrumentation.manuallyInstrument(module);
    expect(() =>
      module.Cerebras.Chat.Completions.prototype.create(body),
    ).toThrow(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(2);
  });

  it("lets the SDK handle invalid request arguments", async () => {
    const create = jest.fn().mockResolvedValue(reply);
    const module = moduleWith(create);
    instrumentation.manuallyInstrument(module);
    await module.Cerebras.Chat.Completions.prototype.create(undefined, {
      timeout: 123,
    });
    expect(create).toHaveBeenCalledWith(undefined, { timeout: 123 });
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("finishes an unexpected response without changing the return value", async () => {
    const module = moduleWith(jest.fn().mockResolvedValue({}));
    instrumentation.manuallyInstrument(module);
    expect(
      await module.Cerebras.Chat.Completions.prototype.create(body),
    ).toEqual({});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
});
