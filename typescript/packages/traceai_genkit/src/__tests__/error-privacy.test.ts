import { ROOT_CONTEXT } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { generateTraceConfig, setAttributes, type TraceConfigOptions } from "@traceai/fi-core";
import { FIGenkitSpanProcessor, hidesFreeText, resolveContentPolicy, SHOW_ALL } from "../index";
import { flowAttributes, v1Span } from "./fixtures";

const CONTENT_FLAGS = [
  ["hideInputs", "FI_HIDE_INPUTS"],
  ["hideOutputs", "FI_HIDE_OUTPUTS"],
  ["hideInputMessages", "FI_HIDE_INPUT_MESSAGES"],
  ["hideOutputMessages", "FI_HIDE_OUTPUT_MESSAGES"],
  ["hideInputText", "FI_HIDE_INPUT_TEXT"],
  ["hideOutputText", "FI_HIDE_OUTPUT_TEXT"],
] as const;
const ENV_KEYS = [...CONTENT_FLAGS.map(([, env]) => env), "FI_HIDE_ERROR_DETAILS", "FI_HIDE_EMBEDDING_VECTORS"];
let savedEnv: Record<string, string | undefined>;
beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
  for (const key of ENV_KEYS) delete process.env[key];
});
afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

describe("Genkit error policy matches fi-core", () => {
  const cases: Array<[string, TraceConfigOptions | undefined, Record<string, string>, boolean]> = [
    ["default", undefined, {}, false],
    ["explicit true", { hideErrorDetails: true }, {}, true],
    ["env true", undefined, { FI_HIDE_ERROR_DETAILS: " TrUe " }, true],
    ["option false wins", { hideInputs: true, hideErrorDetails: false }, { FI_HIDE_ERROR_DETAILS: "true" }, false],
    ["option true wins", { hideErrorDetails: true }, { FI_HIDE_ERROR_DETAILS: "false" }, true],
    ["env false wins", { hideInputs: true }, { FI_HIDE_ERROR_DETAILS: " FALSE " }, false],
    ["invalid env derives", { hideInputText: true }, { FI_HIDE_ERROR_DETAILS: "yes" }, true],
    ["empty env derives", { hideOutputMessages: true }, { FI_HIDE_ERROR_DETAILS: "" }, true],
    ["vectors do not imply hiding", { hideEmbeddingVectors: true }, {}, false],
    ["images do not imply hiding", { hideInputImages: true }, {}, false],
    ...CONTENT_FLAGS.flatMap(([key, env]): typeof cases => [
      [key, { [key]: true }, {}, true],
      [env, undefined, { [env]: "true" }, true],
    ]),
  ];
  it.each(cases)("%s", (_name, config, env, expected) => {
    Object.assign(process.env, env);
    expect(resolveContentPolicy(config).hideErrorDetails).toBe(expected);
    expect(resolveContentPolicy(config).hideErrorDetails).toBe(generateTraceConfig(config).hideErrorDetails);
  });

  it("preserves old public ContentPolicy objects and honors explicit false", () => {
    expect(hidesFreeText(SHOW_ALL)).toBe(false);
    expect(hidesFreeText({ ...SHOW_ALL, hideInputMessages: true })).toBe(true);
    expect(hidesFreeText({ ...SHOW_ALL, hideInputs: true, hideErrorDetails: false })).toBe(false);
  });

  it("derives from the legacy captureContent fallback after option/env precedence", () => {
    const fallback = { hideInputs: true, hideOutputs: true };
    expect(resolveContentPolicy(undefined, fallback).hideErrorDetails).toBe(true);
    process.env.FI_HIDE_INPUTS = "false";
    expect(resolveContentPolicy({ hideOutputs: false }, fallback).hideErrorDetails).toBe(false);
    expect(resolveContentPolicy({ hideErrorDetails: false }, fallback).hideErrorDetails).toBe(false);
  });
});

describe("copied native spans enforce error privacy before export", () => {
  const marker = "SYNTHETIC_PRIVATE_ERROR_MARKER";
  const details = {
    "exception.message": marker,
    "exception.stacktrace": marker,
    "error.message": marker,
    "genkit.exception.message": marker,
    "genkit.exception.stacktrace": marker,
    "genkit.error.message": marker,
    "genkit.tool.error_message": marker,
  };
  const kept = { "exception.type": "TypeError", "exception.escaped": true, "error.type": "INVALID_ARGUMENT", retryable: false };

  it.each([true, false])("hideErrorDetails=%s filters span, context, and all event attributes", async (hidden) => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig: { hideErrorDetails: hidden } });
    try {
      const span = v1Span({
        name: "offline", spanId: "1111111111111111",
        attributes: { ...flowAttributes(), ...details, ...kept },
        status: { code: 2, message: marker },
        events: ["exception", "custom"].map((name) => ({ name, time: [1, 0], attributes: { ...details, ...kept } })),
      });
      const before = JSON.stringify(span);
      processor.onStart(span, setAttributes(ROOT_CONTEXT, { "context.error.message": marker, safe: "metadata" }));
      processor.onEnd(span);
      await processor.forceFlush();
      const [exported] = exporter.getFinishedSpans();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exported.status).toEqual(hidden ? { code: 2 } : { code: 2, message: marker });
      expect(exported.attributes).toMatchObject(kept);
      expect(exported.attributes.safe).toBe("metadata");
      for (const event of exported.events) {
        expect(event.attributes).toMatchObject(kept);
        expect(event.time).toEqual([1, 0]);
      }
      const exportedData = JSON.stringify({ status: exported.status, attributes: exported.attributes, events: exported.events });
      expect(exportedData.includes(marker)).toBe(!hidden);
      expect(JSON.stringify(span)).toBe(before);
      if (hidden) {
        for (const key of [...Object.keys(details), "context.error.message"]) {
          expect(exported.attributes).not.toHaveProperty([key]);
          for (const event of exported.events) expect(event.attributes).not.toHaveProperty([key]);
        }
      } else {
        expect(exported.attributes["context.error.message"]).toBe(marker);
        expect(exported.events[0].attributes).not.toHaveProperty("exception.stacktrace");
        expect(exported.events[0].attributes?.["exception.message"]).toBe(marker);
      }
    } finally {
      await processor.shutdown();
    }
  });
});
