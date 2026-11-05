import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import { FITracer } from "../trace/trace-config/FITracer";
import { TraceConfigOptions } from "../trace/trace-config/types";
import { ROOT_CONTEXT } from "@opentelemetry/api";
import { getPromptTemplate, setPromptTemplate } from "../trace/contextAttributes";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

const retrievalKeys = ["retrieval.documents", "retrieval.documents.0.document.content", "retrieval.documents.0.document.metadata", "retrieval.documents.12.document.content", "retrieval.documents.12.document.metadata"];
const toolSchemaKeys = [SC.LLM_TOOLS, SC.LLM_INVOCATION_PARAMETERS];
const cases: [keyof TraceConfigOptions, string[]][] = [
  ["hideOutputs", [SC.LLM_FUNCTION_CALL, ...retrievalKeys]],
  ["hideInputs", [...toolSchemaKeys, SC.PROMPT_TEMPLATE_VARIABLES, SC.PROMPT_TEMPLATE_TEMPLATE, `${SC.EMBEDDING_EMBEDDINGS}.0.${SC.EMBEDDING_TEXT}`, `${SC.EMBEDDING_EMBEDDINGS}.12.${SC.EMBEDDING_TEXT}`]],
  ["hideInputText", [...toolSchemaKeys, SC.PROMPT_TEMPLATE_VARIABLES, SC.PROMPT_TEMPLATE_TEMPLATE, SC.INPUT_VALUE, SC.RAW_INPUT, `${SC.EMBEDDING_EMBEDDINGS}.0.${SC.EMBEDDING_TEXT}`]],
  ["hideInputImages", [...toolSchemaKeys, SC.PROMPT_TEMPLATE_VARIABLES, SC.PROMPT_TEMPLATE_TEMPLATE, SC.INPUT_VALUE, SC.RAW_INPUT]],
  ["hideInputMessages", [...toolSchemaKeys, SC.PROMPT_TEMPLATE_VARIABLES, SC.PROMPT_TEMPLATE_TEMPLATE, SC.INPUT_VALUE, SC.RAW_INPUT]],
  ["hideOutputText", [SC.LLM_FUNCTION_CALL, SC.OUTPUT_VALUE, SC.RAW_OUTPUT, ...retrievalKeys]],
  ["hideOutputMessages", [SC.LLM_FUNCTION_CALL, SC.OUTPUT_VALUE, SC.RAW_OUTPUT]],
  ["hideEmbeddingVectors", [SC.LLM_FUNCTION_CALL, SC.OUTPUT_VALUE, SC.RAW_OUTPUT, `${SC.EMBEDDING_EMBEDDINGS}.0.${SC.EMBEDDING_VECTOR}`]],
];

describe("privacy covers duplicate payload attributes", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  afterEach(() => exporter.reset());
  afterAll(() => provider.shutdown());

  for (const [flag, keys] of cases) {
    for (const placement of ["creation", "setter", "link"] as const) {
      it(`${flag} protects ${placement} attributes without removing safe metadata`, () => {
        const attributes = Object.fromEntries(keys.map(key => [key, "SYNTHETIC-PRIVATE-PAYLOAD"]));
        const tracer = new FITracer({ tracer: provider.getTracer("privacy"), traceConfig: { [flag]: true } });
        const span = tracer.startSpan("test", {
          attributes: { [SC.LLM_MODEL_NAME]: "synthetic-model", ...(placement === "creation" ? attributes : {}) },
          links: placement === "link" ? [{ context: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 }, attributes }] : [],
        });
        if (placement === "setter") {
          span.setAttributes(attributes);
          for (const [key, value] of Object.entries(attributes)) span.setAttribute(key, value);
        }
        span.end();
        const finished = exporter.getFinishedSpans()[0];
        expect(JSON.stringify({ attributes: finished.attributes, links: finished.links })).not.toContain("SYNTHETIC-PRIVATE-PAYLOAD");
        expect(finished.attributes[SC.LLM_MODEL_NAME]).toBe("synthetic-model");
      });
    }
  }

  const inputFlags = [
    ["hideInputs", "FI_HIDE_INPUTS"],
    ["hideInputText", "FI_HIDE_INPUT_TEXT"],
    ["hideInputImages", "FI_HIDE_INPUT_IMAGES"],
    ["hideInputMessages", "FI_HIDE_INPUT_MESSAGES"],
  ] as const;

  for (const [flag, env] of inputFlags) {
    for (const source of ["options", "environment"] as const) {
      it(`${flag} masks context templates via ${source} without mutating context`, () => {
        const template = { template: "PRIVATE-SYSTEM {question}", variables: { question: "PRIVATE-QUESTION" }, version: "v1" };
        const ctx = setPromptTemplate(ROOT_CONTEXT, template);
        const previous = process.env[env];
        try {
          if (source === "environment") process.env[env] = "true";
          const tracer = new FITracer({
            tracer: provider.getTracer("context-privacy"),
            traceConfig: source === "options" ? { [flag]: true } : undefined,
          });
          tracer.startActiveSpan("context-template", {}, ctx, span => span.end());
          const attributes = exporter.getFinishedSpans()[0].attributes;
          expect(attributes[SC.PROMPT_TEMPLATE_TEMPLATE]).toBe(REDACTED_VALUE);
          expect(attributes[SC.PROMPT_TEMPLATE_VARIABLES]).toBe(REDACTED_VALUE);
          expect(attributes[SC.PROMPT_TEMPLATE_VERSION]).toBe("v1");
          expect(getPromptTemplate(ctx)).toEqual(template);
        } finally {
          if (previous === undefined) delete process.env[env];
          else process.env[env] = previous;
        }
      });
    }
  }

  it("retains context template content by default", () => {
    const template = { template: "PUBLIC-SYSTEM {question}", variables: { question: "PUBLIC-QUESTION" }, version: "v1" };
    const tracer = new FITracer({ tracer: provider.getTracer("context-public"), traceConfig: {} });
    tracer.startActiveSpan("context-template", {}, setPromptTemplate(ROOT_CONTEXT, template), span => span.end());
    const attributes = exporter.getFinishedSpans()[0].attributes;
    expect(attributes[SC.PROMPT_TEMPLATE_TEMPLATE]).toBe(template.template);
    expect(attributes[SC.PROMPT_TEMPLATE_VARIABLES]).toBe(JSON.stringify(template.variables));
    expect(attributes[SC.PROMPT_TEMPLATE_VERSION]).toBe(template.version);
  });

  for (const [flag] of cases) {
    for (const payload of ['{"opaque":"PRIVATE-CONTENT"}', '{not-json:PRIVATE-CONTENT']) {
      it(`${flag} masks opaque copies independently without parsing ${payload}`, () => {
        const hidesInput = inputFlags.some(([inputFlag]) => inputFlag === flag);
        const attributes = {
          [SC.PROMPT_TEMPLATE_TEMPLATE]: payload,
          [SC.LLM_FUNCTION_CALL]: payload,
          // Exact matching must not hide tool-call identifiers in other consumers.
          [`${SC.LLM_FUNCTION_CALL}.id`]: "safe-call-id",
          [SC.LLM_MODEL_NAME]: "synthetic-model",
          [SC.LLM_TOKEN_COUNT_TOTAL]: 15,
        };
        const tracer = new FITracer({ tracer: provider.getTracer("opaque-privacy"), traceConfig: { [flag]: true } });
        tracer.startSpan("opaque", { attributes }).end();
        expect(exporter.getFinishedSpans()[0].attributes).toEqual({
          ...attributes,
          [SC.PROMPT_TEMPLATE_TEMPLATE]: hidesInput ? REDACTED_VALUE : payload,
          [SC.LLM_FUNCTION_CALL]: hidesInput ? payload : REDACTED_VALUE,
        });
        expect(attributes[SC.PROMPT_TEMPLATE_TEMPLATE]).toBe(payload);
        expect(attributes[SC.LLM_FUNCTION_CALL]).toBe(payload);
      });
    }
  }

  it("preserves all payloads with hiding disabled", () => {
    const attributes = Object.fromEntries(cases.flatMap(([, keys]) => keys.map(key => [key, "SYNTHETIC-PUBLIC-PAYLOAD"])));
    const tracer = new FITracer({ tracer: provider.getTracer("privacy"), traceConfig: {} });
    const span = tracer.startSpan("test", { attributes });
    span.end();
    expect(exporter.getFinishedSpans()[0].attributes).toEqual(attributes);
  });
});
