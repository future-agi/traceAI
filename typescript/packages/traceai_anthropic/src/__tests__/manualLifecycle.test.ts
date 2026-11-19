import { afterEach, describe, expect, it, jest } from "@jest/globals";
import Anthropic from "@anthropic-ai/sdk";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { AnthropicInstrumentation, isPatched } from "../instrumentation";

const params = {
  model: "synthetic-model",
  max_tokens: 8,
  messages: [{ role: "user" as const, content: "MANUAL_INPUT_CANARY" }],
};
const response = {
  id: "msg_manual_fixture",
  type: "message",
  role: "assistant",
  model: params.model,
  content: [{ type: "text", text: "MANUAL_OUTPUT_CANARY" }],
  stop_reason: "end_turn",
  stop_sequence: null,
  usage: { input_tokens: 3, output_tokens: 2 },
};

// These concrete targets never enter the CommonJS loader registry, just like
// the documented native-ESM manual setup. No SDK clients or network are used.
function manualSDK() {
  class Messages {
    calls: unknown[][] = [];
    create(...args: unknown[]) {
      this.calls.push(args);
      return Promise.resolve(response);
    }
  }
  return class LegacySDK {
    static Messages = Messages;
  };
}

function sdkModule(module: unknown): typeof Anthropic {
  return module as typeof Anthropic;
}

class TestInstrumentation extends AnthropicInstrumentation {
  // Exercise the same callback a cached CommonJS loader entry would invoke.
  loaderUnpatch(module: typeof Anthropic) {
    this.init().unpatch?.(module);
  }
}

const instruments: AnthropicInstrumentation[] = [];
const providers: BasicTracerProvider[] = [];
function tracerProvider() {
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  providers.push(provider);
  return { memory, provider };
}
function instrument(hideContent = false) {
  const { memory, provider } = tracerProvider();
  const instrumentation = new TestInstrumentation({
    instrumentationConfig: { enabled: false },
    traceConfig: { hideInputs: hideContent, hideOutputs: hideContent },
  });
  instruments.push(instrumentation);
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  return { instrumentation, memory };
}

afterEach(async () => {
  for (const instrumentation of instruments.splice(0)) instrumentation.disable();
  for (const provider of providers.splice(0)) await provider.shutdown();
});

describe("real SDK patch generations", () => {
  it.each([false, true])("records one SDK call once after re-enable (outer wrapper=%s)", async (outerWrapper) => {
    const sdkCreate = jest.spyOn(Anthropic.Messages.prototype, "create");
    const syntheticFetch = jest.fn(async () => new Response(JSON.stringify(response), {
      status: 200,
      headers: { "content-type": "application/json", "request-id": "req_lifecycle_fixture" },
    }));
    const client = new Anthropic({
      apiKey: "synthetic",
      baseURL: "https://offline.invalid",
      maxRetries: 0,
      fetch: syntheticFetch as any,
    });
    const { instrumentation, memory } = instrument();
    try {
      instrumentation.manuallyInstrument(Anthropic);
      const retained = Anthropic.Messages.prototype.create;
      const delegate = function (this: Anthropic.Messages, ...args: Parameters<Anthropic.Messages["create"]>) {
        return retained.apply(this, args);
      } as Anthropic.Messages["create"];
      if (outerWrapper) Anthropic.Messages.prototype.create = delegate;

      await expect(client.messages.create(params)).resolves.toEqual(response);
      expect(sdkCreate).toHaveBeenCalledTimes(1);
      expect(syntheticFetch).toHaveBeenCalledTimes(1);
      expect(memory.getFinishedSpans()).toHaveLength(1);

      for (let cycle = 0; cycle < 2; cycle++) {
        instrumentation.disable();
        expect(Anthropic.Messages.prototype.create).toBe(outerWrapper ? delegate : sdkCreate);
        expect(isPatched()).toBe(false);
        sdkCreate.mockClear();
        syntheticFetch.mockClear();
        memory.reset();
        await expect(client.messages.create(params)).resolves.toEqual(response);
        expect(sdkCreate).toHaveBeenCalledTimes(1);
        expect(syntheticFetch).toHaveBeenCalledTimes(1);
        expect(memory.getFinishedSpans()).toHaveLength(0);

        instrumentation.enable();
        const active = Anthropic.Messages.prototype.create;
        instrumentation.enable();
        instrumentation.manuallyInstrument(Anthropic);
        expect(Anthropic.Messages.prototype.create).toBe(active);
        sdkCreate.mockClear();
        syntheticFetch.mockClear();
        const promise = client.messages.create(params);
        expect(typeof promise.withResponse).toBe("function");
        const result = await promise.withResponse();
        expect(result.data).toEqual(response);
        expect(result.response.status).toBe(200);
        expect(sdkCreate).toHaveBeenCalledTimes(1);
        expect(syntheticFetch).toHaveBeenCalledTimes(1);
        expect(memory.getFinishedSpans()).toHaveLength(1);
      }
    } finally {
      instrumentation.disable();
      sdkCreate.mockRestore();
    }
  });

  it("keeps a directly retained generation inactive after re-enable", async () => {
    const sdkCreate = jest.spyOn(Anthropic.Messages.prototype, "create");
    const syntheticFetch = jest.fn(async () => new Response(JSON.stringify(response), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    const client = new Anthropic({ apiKey: "synthetic", maxRetries: 0, fetch: syntheticFetch as any });
    const { instrumentation, memory } = instrument();
    try {
      instrumentation.manuallyInstrument(Anthropic);
      const retained = Anthropic.Messages.prototype.create;
      instrumentation.disable();
      instrumentation.enable();
      await expect(retained.call(client.messages, params)).resolves.toEqual(response);
      expect(sdkCreate).toHaveBeenCalledTimes(1);
      expect(syntheticFetch).toHaveBeenCalledTimes(1);
      expect(memory.getFinishedSpans()).toHaveLength(0);
      await expect(client.messages.create(params)).resolves.toEqual(response);
      expect(sdkCreate).toHaveBeenCalledTimes(2);
      expect(syntheticFetch).toHaveBeenCalledTimes(2);
      expect(memory.getFinishedSpans()).toHaveLength(1);
    } finally {
      instrumentation.disable();
      sdkCreate.mockRestore();
    }
  });
});

describe("manual ESM lifecycle", () => {
  it("restores create and stops spans after disabling a manual-only target", async () => {
    const SDK = manualSDK();
    const original = SDK.Messages.prototype.create;
    const { instrumentation, memory } = instrument();
    const messages = new SDK.Messages();
    const options = { timeout: 123 };

    instrumentation.manuallyInstrument(sdkModule(SDK));
    const retainedWrapper = SDK.Messages.prototype.create;
    expect(retainedWrapper).not.toBe(original);
    await expect(messages.create(params, options)).resolves.toBe(response);
    expect(memory.getFinishedSpans()).toHaveLength(1);

    instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);
    expect(isPatched()).toBe(false);
    expect(SDK).toHaveProperty("fiPatched", false);
    await expect(messages.create(params, options)).resolves.toBe(response);
    await expect(retainedWrapper.call(messages, params, options)).resolves.toBe(response);
    expect(messages.calls).toEqual([[params, options], [params, options], [params, options]]);
    expect(memory.getFinishedSpans()).toHaveLength(1);

    instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);
  });

  it("re-enables the manual target once with a replacement tracer provider", async () => {
    const SDK = manualSDK();
    const original = SDK.Messages.prototype.create;
    const { instrumentation, memory } = instrument();
    const messages = new SDK.Messages();
    instrumentation.manuallyInstrument(sdkModule(SDK));
    await messages.create(params);
    instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);

    const replacement = tracerProvider();
    instrumentation.setTracerProvider(replacement.provider);
    instrumentation.enable();
    expect(SDK.Messages.prototype.create).not.toBe(original);
    const wrapped = SDK.Messages.prototype.create;
    instrumentation.manuallyInstrument(sdkModule(SDK));
    instrumentation.enable();
    expect(SDK.Messages.prototype.create).toBe(wrapped);
    await expect(messages.create(params)).resolves.toBe(response);
    expect(memory.getFinishedSpans()).toHaveLength(1);
    expect(replacement.memory.getFinishedSpans()).toHaveLength(1);
    instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);
    expect(isPatched()).toBe(false);
  });

  it("hands off privacy configuration without letting a non-owner remove the patch", async () => {
    const SDK = manualSDK();
    const original = SDK.Messages.prototype.create;
    const first = instrument();
    const replacement = instrument(true);
    const alias = { default: SDK };
    const messages = new SDK.Messages();
    first.instrumentation.manuallyInstrument(sdkModule(SDK));
    const firstWrapper = SDK.Messages.prototype.create;
    replacement.instrumentation.manuallyInstrument(sdkModule(alias));
    replacement.instrumentation.loaderUnpatch(sdkModule(SDK));
    replacement.instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(firstWrapper);
    expect(isPatched()).toBe(true);
    await messages.create(params);
    expect(first.memory.getFinishedSpans()).toHaveLength(1);
    expect(replacement.memory.getFinishedSpans()).toHaveLength(0);
    expect(JSON.stringify(first.memory.getFinishedSpans()[0].attributes)).toContain("MANUAL_INPUT_CANARY");

    first.instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);
    replacement.instrumentation.enable();
    const replacementWrapper = SDK.Messages.prototype.create;
    expect(replacementWrapper).not.toBe(original);
    first.instrumentation.loaderUnpatch(sdkModule(SDK));
    first.instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(replacementWrapper);
    expect(isPatched()).toBe(true);
    await expect(messages.create(params)).resolves.toBe(response);
    expect(first.memory.getFinishedSpans()).toHaveLength(1);
    expect(replacement.memory.getFinishedSpans()).toHaveLength(1);
    const attributes = JSON.stringify(replacement.memory.getFinishedSpans()[0].attributes);
    expect(attributes).not.toContain("MANUAL_INPUT_CANARY");
    expect(attributes).not.toContain("MANUAL_OUTPUT_CANARY");
    replacement.instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);
    expect(isPatched()).toBe(false);
  });

  it("cleans up frozen namespace targets and deduplicates aliases by prototype", async () => {
    const SDK = manualSDK();
    const original = SDK.Messages.prototype.create;
    const namespace = Object.freeze({ default: SDK });
    const { instrumentation, memory } = instrument();
    instrumentation.manuallyInstrument(sdkModule(namespace));
    const wrapped = SDK.Messages.prototype.create;
    expect(wrapped).not.toBe(original);
    instrumentation.manuallyInstrument(sdkModule(SDK));
    expect(SDK.Messages.prototype.create).toBe(wrapped);
    await new SDK.Messages().create(params);
    expect(memory.getFinishedSpans()).toHaveLength(1);
    instrumentation.disable();
    expect(SDK.Messages.prototype.create).toBe(original);
    expect(isPatched()).toBe(false);
  });

  it("cleans up every owned target while preserving another owner's target", async () => {
    const firstSDK = manualSDK();
    const secondSDK = manualSDK();
    const otherSDK = manualSDK();
    const originals = [firstSDK, secondSDK, otherSDK].map(SDK => SDK.Messages.prototype.create);
    const first = instrument();
    const other = instrument();
    first.instrumentation.manuallyInstrument(sdkModule(firstSDK));
    first.instrumentation.manuallyInstrument(sdkModule(secondSDK));
    other.instrumentation.manuallyInstrument(sdkModule(otherSDK));
    for (const [index, SDK] of [firstSDK, secondSDK, otherSDK].entries()) {
      expect(SDK.Messages.prototype.create).not.toBe(originals[index]);
      await new SDK.Messages().create(params);
    }
    expect(first.memory.getFinishedSpans()).toHaveLength(2);
    expect(other.memory.getFinishedSpans()).toHaveLength(1);
    first.instrumentation.disable();
    expect(firstSDK.Messages.prototype.create).toBe(originals[0]);
    expect(secondSDK.Messages.prototype.create).toBe(originals[1]);
    expect(otherSDK.Messages.prototype.create).not.toBe(originals[2]);
    expect(isPatched()).toBe(true);
    await new otherSDK.Messages().create(params);
    expect(other.memory.getFinishedSpans()).toHaveLength(2);
    other.instrumentation.disable();
    expect(otherSDK.Messages.prototype.create).toBe(originals[2]);
    expect(isPatched()).toBe(false);
  });
});
