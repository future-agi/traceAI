import { describe, it, expect, beforeAll, afterAll, beforeEach, afterEach } from "@jest/globals";
import { Attributes, context, SpanStatusCode } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import {
  DEFAULT_BASE64_IMAGE_MAX_LENGTH,
  FI_BASE64_IMAGE_MAX_LENGTH,
  REDACTED_VALUE,
} from "../trace/trace-config/constants";
import { TraceConfigOptions } from "../trace/trace-config/types";
import { capBase64Images } from "../trace/trace-config/imageCap";

/**
 * base64ImageMaxLength / FI_BASE64_IMAGE_MAX_LENGTH: a base64 image longer than the limit is
 * replaced, whole, by the redaction marker, by value and whatever the attribute key, in every
 * exported attribute, event and status. One case per shape an instrumentation exports (source
 * lines cited), plus the boundary: F2 alone preserves the exact limit, while composed I1
 * export omits image bytes and preserves non-image siblings at every positive cap.
 */

const MAX = 1000;
/** A synthetic payload marker: base64 letters, so it is part of the base64 text */
const CANARY = "CANARYxPAYLOADxBYTES";
/** base64 of the PNG signature and the start of an IHDR chunk */
const PNG_HEAD = "iVBORw0KGgoAAAANSUhEUg";

/** Base64 text of exactly `length` characters: an optional head, the canary, then padding */
function base64Of(length: number, head = ""): string {
  const start = head + CANARY;
  return start + "A".repeat(length - start.length);
}

/** A base64 PNG data URL of exactly `length` characters */
function dataUrl(length: number): string {
  const prefix = "data:image/png;base64,";
  return prefix + base64Of(length - prefix.length, PNG_HEAD);
}

/** Everything the exporter is handed for the span, as text */
function exported(span: ReadableSpan): string {
  return JSON.stringify({
    name: span.name,
    attributes: span.attributes,
    events: span.events,
    status: span.status,
    links: span.links,
  });
}

function expectNoImage(span: ReadableSpan) {
  const text = exported(span);
  expect(text).not.toContain(CANARY);
  expect(text).not.toContain("base64,");
  expect(text).not.toContain(PNG_HEAD);
}

describe("base64 image cap", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const contextManager = new AsyncLocalStorageContextManager();

  beforeAll(() => {
    context.setGlobalContextManager(contextManager.enable());
  });
  afterAll(() => {
    context.disable();
  });
  beforeEach(() => {
    exporter.reset();
  });

  function tracerWith(traceConfig: TraceConfigOptions = { base64ImageMaxLength: MAX }) {
    return new FITracer({ tracer: provider.getTracer("test"), traceConfig });
  }

  function onlySpan(): ReadableSpan {
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  /** Exports one span with the attributes set at creation and again with setAttributes */
  function exportWith(attributes: Attributes, traceConfig?: TraceConfigOptions): ReadableSpan {
    const span = tracerWith(traceConfig).startSpan("llm", { attributes });
    span.setAttributes({ ...attributes });
    span.end();
    return onlySpan();
  }

  // One case per instrumentation shape: [label, source, attributes for an image of `length`]
  const SHAPES: Array<[string, (length: number) => Attributes]> = [
    // traceai_openai/src/instrumentation.ts:789,886-887 (messages) and the request body copies
    ["OpenAI chat image_url.url in gen_ai.input.messages, input.value and raw.input", (length) => {
      const messages = [{ role: "user", content: [
        { type: "text", text: "what is this?" },
        { type: "image_url", image_url: { url: dataUrl(length) } },
      ] }];
      const body = JSON.stringify({ model: "gpt-x", messages });
      return {
        "gen_ai.input.messages": JSON.stringify(messages),
        "input.value": body,
        "input.mime_type": "application/json",
        "raw.input": body,
      };
    }],
    // traceai_openai/src/responseAttributes.ts:101,123 (input_image with a string image_url)
    ["OpenAI Responses input_image image_url string", (length) => ({
      "gen_ai.input.messages": JSON.stringify([
        { role: "user", content: [{ type: "input_image", image_url: dataUrl(length) }] },
      ]),
    })],
    // traceai_openai/src/responseAttributes.ts:70 (computer_call_output screenshot)
    ["OpenAI Responses computer screenshot", (length) => ({
      "gen_ai.input.messages": JSON.stringify([
        { role: "tool", content: [{ type: "input_image", image_url: dataUrl(length) }] },
      ]),
    })],
    // traceai_anthropic/src/responseAttributes.ts:30-31 (raw base64 under a declared media_type)
    ["Anthropic image source.data (raw base64, no signature)", (length) => ({
      "gen_ai.input.messages": JSON.stringify([{ role: "user", content: [
        { type: "image", source: { type: "base64", media_type: "image/png", data: base64Of(length) } },
      ] }]),
    })],
    // traceai_bedrock/src/attributes/attribute-helpers.ts:209-215 (flattened key, whole value)
    ["Bedrock converse flattened message_content.image.image.url", (length) => ({
      "gen_ai.input.messages.0.message.contents.1.message_content.image.image.url": dataUrl(length),
    })],
    // traceai_bedrock/src/attributes/invoke-model-helpers.ts:242-246,383-387
    ["Bedrock invoke-model image source.data", (length) => ({
      "input.value": JSON.stringify({ messages: [{ role: "user", content: [
        { type: "image", source: { type: "base64", media_type: "image/jpeg", data: base64Of(length) } },
      ] }] }),
    })],
    // traceai_bedrock/src/attributes/invoke-model-request-attributes.ts:56-57 (data URL in text)
    ["Bedrock invoke-model [image: data URL] inside text", (length) => ({
      "gen_ai.input.messages": JSON.stringify([
        { role: "user", content: `describe [image: ${dataUrl(length)}] please` },
      ]),
      "input.value": `describe [image: ${dataUrl(length)}] please`,
    })],
    // traceai_google_genai/src/instrumentation.ts:65-69 (inlineData with mimeType)
    ["Google GenAI inlineData.data", (length) => ({
      "input.value": JSON.stringify({ contents: [{ role: "user", parts: [
        { inlineData: { mimeType: "image/png", data: base64Of(length) } },
      ] }] }),
    })],
    // OTel GenAI semantic conventions: a blob part with image modality
    ["OTel GenAI blob part with image modality", (length) => ({
      "gen_ai.input.messages": JSON.stringify([{ role: "user", parts: [
        { type: "blob", modality: "image", mime_type: "image/png", content: base64Of(length) },
      ] }]),
    })],
    // A bare base64 PNG with no declaring field (e.g. a source.bytes copy)
    ["bare base64 PNG, whole value and inside JSON", (length) => ({
      "custom.image": base64Of(length, PNG_HEAD),
      "tool.output": JSON.stringify({ image: { source: { bytes: base64Of(length, PNG_HEAD) } } }),
    })],
    // A stringified body inside a message, and a double-encoded body
    ["JSON nested in a JSON string, and a double-encoded body", (length) => {
      const body = JSON.stringify({ image_url: { url: dataUrl(length) } });
      return {
        "gen_ai.input.messages": JSON.stringify([{ role: "user", content: body }]),
        "raw.input": JSON.stringify(body),
      };
    }],
  ];

  describe.each(SHAPES)("%s", (_label, shape) => {
    it("longer than the limit: only the marker is exported, no prefix or part of the payload", () => {
      const span = exportWith(shape(MAX + 1));
      expectNoImage(span);
      expect(exported(span)).toContain(REDACTED_VALUE);
    });

    it("exactly the limit: I1 omits image payloads and retains non-image attributes", () => {
      const attributes = shape(MAX);
      // PENDING I1xF2-PRECEDENCE T12-T22: TS I1 media omission dominates positive caps.
      // Literal fixture
      // replacements retain every other byte, including nested JSON and metadata.
      const expected = Object.fromEntries(Object.entries(attributes).map(([key, value]) => {
        if (typeof value !== "string") return [key, value];
        expect(capBase64Images(value, MAX)).toBe(value);
        return [key, value
          .split(dataUrl(MAX)).join("<omitted: 978 base64 chars>")
          .split(base64Of(MAX, PNG_HEAD)).join("<omitted: 1000 base64 chars>")
          .split(base64Of(MAX)).join("<omitted: 1000 base64 chars>")];
      }));
      const span = exportWith(attributes);
      expect(span.attributes).toEqual(expected);
      expectNoImage(span);
    });
  });

  it("a whole-value data URL becomes exactly the marker, as in Python", () => {
    const key = "gen_ai.input.messages.0.message.contents.1.message_content.image.image.url";
    expect(exportWith({ [key]: dataUrl(MAX + 1) }).attributes[key]).toBe(REDACTED_VALUE);
  });

  it("only the image is replaced: the rest of the message is kept", () => {
    const messages = [{ role: "user", content: [
      { type: "text", text: "what is this?" },
      { type: "image_url", image_url: { url: dataUrl(MAX + 1) } },
    ] }];
    const exportedMessages = exportWith({ "gen_ai.input.messages": JSON.stringify(messages) })
      .attributes["gen_ai.input.messages"];
    expect(JSON.parse(exportedMessages as string)).toEqual([{ role: "user", content: [
      { type: "text", text: "what is this?" },
      { type: "image_url", image_url: { url: REDACTED_VALUE } },
    ] }]);
  });

  it("caps images in event attributes, the recorded exception and the ERROR status", () => {
    const span = tracerWith().startSpan("llm");
    span.addEvent("request", { "request.body": JSON.stringify({ url: dataUrl(MAX + 1) }) });
    span.recordException(new Error(`400 invalid image ${dataUrl(MAX + 1)}`));
    span.setStatus({ code: SpanStatusCode.ERROR, message: `400 invalid image ${dataUrl(MAX + 1)}` });
    span.end();
    const exportedSpan = onlySpan();
    expectNoImage(exportedSpan);
    expect(exportedSpan.status.message).toBe(`400 invalid image ${REDACTED_VALUE}`);
  });

  it("unparseable JSON (a truncated body): the embedded data URL is scanned, fails closed above the limit", () => {
    const truncated = (length: number) => `{"messages":[{"content":[{"image_url":{"url":"${dataUrl(length)}`;
    expectNoImage(exportWith({ "input.value": truncated(MAX + 1) }));
    exporter.reset();
    // PENDING I1xF2-PRECEDENCE: I1 incomplete JSON refusal follows the image-cap mechanism.
    expect(capBase64Images(truncated(MAX), MAX)).toBe(truncated(MAX));
    expect(exportWith({ "input.value": truncated(MAX) }).attributes["input.value"]).toBe("<omitted: incomplete audio>");
  });

  it("string array attributes are capped per element", () => {
    const span = exportWith({ "input.images": [dataUrl(MAX + 1), "https://example.com/a.png"] });
    expect(span.attributes["input.images"]).toEqual([REDACTED_VALUE, "https://example.com/a.png"]);
  });

  it("runs before PII redaction, so no rewritten part of an image is exported", () => {
    const key = "gen_ai.input.messages.0.message.contents.0.message_content.image.image.url";
    const value = `data:image/png;base64,${PNG_HEAD}4111111111111111${"A".repeat(MAX)}`;
    const span = exportWith({ [key]: value }, { base64ImageMaxLength: MAX, piiRedaction: true });
    expect(span.attributes[key]).toBe(REDACTED_VALUE);
  });

  describe("not capped", () => {
    it("a long https image URL", () => {
      const url = `https://example.com/${"a".repeat(MAX * 2)}.png`;
      const attributes = {
        "gen_ai.input.messages": JSON.stringify([
          { role: "user", content: [{ type: "image_url", image_url: { url } }] },
        ]),
      };
      expect(exportWith(attributes).attributes).toEqual(attributes);
    });

    it("base64 that is not an image (audio): disclosed, not capped", () => {
      const attributes = {
        "gen_ai.input.messages": JSON.stringify([{ role: "user", content: [
          { type: "input_audio", input_audio: { format: "wav", data: base64Of(MAX * 2) } },
        ] }]),
      };
      // PENDING I1xF2-PRECEDENCE: audio bypasses the image cap, not the I1 media policy.
      expect(capBase64Images(attributes["gen_ai.input.messages"], MAX)).toBe(attributes["gen_ai.input.messages"]);
      expect(exportWith(attributes).attributes).toEqual({
        "gen_ai.input.messages": JSON.stringify([{ role: "user", content: [
          { type: "input_audio", input_audio: { format: "wav", data: "<omitted: 2000 base64 chars>" } },
        ] }]),
      });
    });

    it("long text that is not base64", () => {
      const attributes = { "input.value": `${"lorem ipsum ".repeat(MAX)}` };
      expect(exportWith(attributes).attributes).toEqual(attributes);
    });

    it("a data URL that is not base64", () => {
      const attributes = { "input.value": `data:image/svg+xml,${"%3Csvg%3E".repeat(MAX)}` };
      // PENDING I1xF2-PRECEDENCE: non-base64 data URIs retain I1 omission at export.
      expect(capBase64Images(attributes["input.value"], MAX)).toBe(attributes["input.value"]);
      expect(exportWith(attributes).attributes).toEqual({ "input.value": "<omitted: data URI>" });
    });
  });

  it("hideInputImages still removes input images (the cap does not bring them back)", () => {
    const messages = [{ role: "user", content: [
      { type: "text", text: "what is this?" },
      { type: "image_url", image_url: { url: dataUrl(MAX - 100) } },
    ] }];
    const span = exportWith(
      { "gen_ai.input.messages": JSON.stringify(messages), "input.value": JSON.stringify({ messages }) },
      { base64ImageMaxLength: MAX, hideInputImages: true },
    );
    expectNoImage(span);
    expect(span.attributes["input.value"]).toBe(REDACTED_VALUE);
  });

  describe("the limit", () => {
    const saved = process.env[FI_BASE64_IMAGE_MAX_LENGTH];
    afterEach(() => {
      if (saved === undefined) delete process.env[FI_BASE64_IMAGE_MAX_LENGTH];
      else process.env[FI_BASE64_IMAGE_MAX_LENGTH] = saved;
    });

    it("defaults to DEFAULT_BASE64_IMAGE_MAX_LENGTH (0)", () => {
      delete process.env[FI_BASE64_IMAGE_MAX_LENGTH];
      expect(DEFAULT_BASE64_IMAGE_MAX_LENGTH).toBe(0);
      const key = "gen_ai.input.messages.0.message.contents.0.message_content.image.image.url";
      const at = "";
      expect(exportWith({ [key]: at }, {}).attributes[key]).toBe(at);
      exporter.reset();
      expect(exportWith({ [key]: "data:image/png;base64,A" }, {}).attributes[key])
        .toBe("[image-redacted]");
    });

    it("is read from FI_BASE64_IMAGE_MAX_LENGTH", () => {
      process.env[FI_BASE64_IMAGE_MAX_LENGTH] = String(MAX);
      const key = "custom.image";
      expect(exportWith({ [key]: dataUrl(MAX + 1) }, {}).attributes[key]).toBe(REDACTED_VALUE);
      exporter.reset();
      // PENDING I1xF2-PRECEDENCE T26: TS I1 media omission dominates positive caps.
      const span = exportWith({ [key]: dataUrl(MAX) }, {});
      expect(span.attributes[key]).toBe("<omitted: 978 base64 chars>");
      expectNoImage(span);
    });

    it("the option wins over the environment variable", () => {
      process.env[FI_BASE64_IMAGE_MAX_LENGTH] = String(MAX);
      const key = "custom.image";
      const value = dataUrl(MAX + 1);
      // PENDING I1xF2-PRECEDENCE T27: TS I1 media omission dominates positive caps.
      const span = exportWith({ [key]: value }, { base64ImageMaxLength: MAX * 2 });
      expect(span.attributes[key]).toBe("<omitted: 979 base64 chars>");
      expectNoImage(span);
    });
  });
});
