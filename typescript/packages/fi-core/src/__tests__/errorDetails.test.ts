import { describe, it, expect, beforeAll, afterAll, beforeEach, afterEach } from "@jest/globals";
import { context, SpanStatusCode, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import {
  FI_HIDE_ERROR_DETAILS,
  FI_HIDE_INPUTS,
  HIDE_ERROR_DETAILS_IMPLIED_BY,
  traceConfigMetadata,
} from "../trace/trace-config/constants";
import {
  ERROR_MESSAGE,
  ERROR_TYPE,
  EXCEPTION_ESCAPED,
  EXCEPTION_MESSAGE,
  EXCEPTION_STACKTRACE,
  EXCEPTION_TYPE,
  getErrorMessage,
  getErrorType,
  getExceptionType,
  stripStackTrace,
} from "../trace/trace-config/errorDetails";
import { TraceConfigOptions } from "../trace/trace-config/types";

const FI_ENV_KEYS = Object.values(traceConfigMetadata).map((m) => m.envKey);

/** Mimics an Anthropic SDK error: `name` stays "Error", the body is on `error`, no `code` */
class AnthropicNotFoundError extends Error {
  status = 404;
  error = { type: "error", error: { type: "not_found_error", message: "model: x" } };
}

/** Mimics an OpenAI SDK error: a subclass whose `name` stays "Error", with a provider `code` */
class NotFoundError extends Error {
  status = 404;
  code: string | undefined;
  constructor(message: string, code?: string) {
    super(message);
    this.code = code;
  }
}

const PROMPT = "my secret prompt: the launch code is 0000";

function providerError() {
  // Provider error bodies can quote the prompt back
  return new NotFoundError(`404 model not found for input "${PROMPT}"`, "model_not_found");
}

describe("hideErrorDetails derivation", () => {
  const savedEnv: Record<string, string | undefined> = {};
  beforeEach(() => {
    for (const key of FI_ENV_KEYS) {
      savedEnv[key] = process.env[key];
      delete process.env[key];
    }
  });
  afterEach(() => {
    for (const key of FI_ENV_KEYS) {
      if (savedEnv[key] === undefined) delete process.env[key];
      else process.env[key] = savedEnv[key];
    }
  });

  it("is off by default", () => {
    expect(generateTraceConfig().hideErrorDetails).toBe(false);
    expect(generateTraceConfig({}).hideErrorDetails).toBe(false);
  });

  it.each(HIDE_ERROR_DETAILS_IMPLIED_BY.map((flag) => [flag]))(
    "is implied by %s",
    (flag) => {
      expect(
        generateTraceConfig({ [flag]: true } as TraceConfigOptions).hideErrorDetails,
      ).toBe(true);
    },
  );

  it("covers exactly the six content-hiding flags", () => {
    expect([...HIDE_ERROR_DETAILS_IMPLIED_BY].sort()).toEqual(
      [
        "hideInputMessages",
        "hideInputText",
        "hideInputs",
        "hideOutputMessages",
        "hideOutputText",
        "hideOutputs",
      ].sort(),
    );
  });

  it.each([["hideInputImages"], ["hideEmbeddingVectors"], ["piiRedaction"]])(
    "is not implied by %s",
    (flag) => {
      expect(
        generateTraceConfig({ [flag]: true } as TraceConfigOptions).hideErrorDetails,
      ).toBe(false);
    },
  );

  it.each(HIDE_ERROR_DETAILS_IMPLIED_BY.map((flag) => [flag]))(
    "an explicit false wins over %s",
    (flag) => {
      expect(
        generateTraceConfig({
          [flag]: true,
          hideErrorDetails: false,
        } as TraceConfigOptions).hideErrorDetails,
      ).toBe(false);
    },
  );

  it("an explicit true works on its own", () => {
    const config = generateTraceConfig({ hideErrorDetails: true });
    expect(config.hideErrorDetails).toBe(true);
    expect(config.hideInputs).toBe(false);
  });

  it("is derived from env hide flags even when no options are passed", () => {
    process.env[FI_HIDE_INPUTS] = "true";
    expect(generateTraceConfig().hideInputs).toBe(true);
    expect(generateTraceConfig().hideErrorDetails).toBe(true);
  });

  it("FI_HIDE_ERROR_DETAILS=false wins over a derived true", () => {
    process.env[FI_HIDE_INPUTS] = "true";
    process.env[FI_HIDE_ERROR_DETAILS] = "false";
    expect(generateTraceConfig().hideErrorDetails).toBe(false);
  });

  it("FI_HIDE_ERROR_DETAILS=true works on its own (case-insensitive)", () => {
    process.env[FI_HIDE_ERROR_DETAILS] = " TRUE ";
    expect(generateTraceConfig().hideErrorDetails).toBe(true);
  });

  it("an unparseable FI_HIDE_ERROR_DETAILS falls back to the derived value", () => {
    process.env[FI_HIDE_ERROR_DETAILS] = "yes please";
    expect(generateTraceConfig().hideErrorDetails).toBe(false);
    process.env[FI_HIDE_INPUTS] = "true";
    expect(generateTraceConfig().hideErrorDetails).toBe(true);
  });

  it("the constructor option wins over FI_HIDE_ERROR_DETAILS", () => {
    process.env[FI_HIDE_ERROR_DETAILS] = "true";
    expect(generateTraceConfig({ hideErrorDetails: false }).hideErrorDetails).toBe(false);
    process.env[FI_HIDE_ERROR_DETAILS] = "false";
    expect(generateTraceConfig({ hideErrorDetails: true }).hideErrorDetails).toBe(true);
  });
});

describe("error detail helpers", () => {
  it("getErrorType prefers a provider error code", () => {
    expect(getErrorType(providerError())).toBe("model_not_found");
  });

  it("getErrorType falls back to the class name", () => {
    expect(getErrorType(new NotFoundError("x"))).toBe("NotFoundError");
    expect(getErrorType(new TypeError("x"))).toBe("TypeError");
    expect(getErrorType(new Error("x"))).toBe("Error");
    const named = new Error("x");
    named.name = "ValidationException";
    expect(getErrorType(named)).toBe("ValidationException");
  });

  it("getErrorType ignores codes that are prose, not identifiers", () => {
    const err = Object.assign(new RangeError("x"), { code: "this is not a code" });
    expect(getErrorType(err)).toBe("RangeError");
    // HTTP status-like codes are not error codes either
    expect(getErrorType(Object.assign(new RangeError("x"), { code: "404" }))).toBe("RangeError");
  });

  it("getErrorType reads the provider code from the common SDK shapes", () => {
    // Anthropic: the body is on `error`
    expect(getErrorType(new AnthropicNotFoundError("404 model: x"))).toBe("not_found_error");
    // OpenAI-compatible error without a code: `type`
    const typed = Object.assign(new NotFoundError("x"), { type: "invalid_request_error" });
    expect(getErrorType(typed)).toBe("invalid_request_error");
    // Node system errors
    expect(getErrorType(Object.assign(new Error("x"), { code: "ECONNREFUSED" }))).toBe(
      "ECONNREFUSED",
    );
  });

  it("getExceptionType is the string code or the error name, as the OpenTelemetry SDK records", () => {
    expect(getExceptionType(providerError())).toBe("model_not_found");
    // The SDK records `name` ("Error") for the Anthropic error, not the body type
    expect(getExceptionType(new AnthropicNotFoundError("x"))).toBe("Error");
    expect(getExceptionType(new TypeError("x"))).toBe("TypeError");
    expect(getExceptionType({ name: "AgentSpanError", message: "x" })).toBe("AgentSpanError");
    expect(getExceptionType("boom")).toBe("_OTHER");
  });

  it("getExceptionType uses the error name / class for a numeric or non-identifier code", () => {
    // The SDK alone would record "400"; Python records the class
    expect(getExceptionType(Object.assign(new Error("x"), { code: 400 }))).toBe("Error");
    expect(getExceptionType(Object.assign(new RangeError("x"), { code: 400 }))).toBe("RangeError");
    expect(getExceptionType(Object.assign(new TypeError("x"), { code: "not a code" }))).toBe(
      "TypeError",
    );
    const nameless = Object.assign(new NotFoundError("x"), { code: 404, name: "" });
    expect(getExceptionType(nameless)).toBe("NotFoundError");
  });

  it("getErrorType returns _OTHER when nothing is known", () => {
    expect(getErrorType("boom")).toBe("_OTHER");
    expect(getErrorType({ message: "boom" })).toBe("_OTHER");
    expect(getErrorType(undefined)).toBe("_OTHER");
  });

  it("getErrorMessage returns the message only", () => {
    const err = new Error("only this");
    expect(getErrorMessage(err)).toBe("only this");
    expect(getErrorMessage("plain")).toBe("plain");
    expect(getErrorMessage({})).toBeUndefined();
  });

  it("stripStackTrace removes a JS stack", () => {
    const err = new Error("request failed");
    expect(err.stack).toContain("\n    at ");
    expect(stripStackTrace(err.stack as string)).toBe("Error: request failed");
  });

  it("stripStackTrace removes the stack from LangChain's run.error", () => {
    const err = new Error("request failed");
    const runError = `${err.message}\n\n${err.stack}`;
    expect(stripStackTrace(runError)).toBe("request failed");
  });

  it("stripStackTrace removes a relayed Python traceback and keeps its final line", () => {
    const message =
      'upstream error\nTraceback (most recent call last):\n  File "/private/tmp/app.py", line 1, in <module>\nValueError: x';
    expect(stripStackTrace(message)).toBe("upstream error\nValueError: x");
  });

  it("stripStackTrace keeps 'Type: msg' when the whole message is a Python traceback", () => {
    const traceback = [
      "Traceback (most recent call last):",
      '  File "/private/tmp/app.py", line 3, in <module>',
      "    boom()",
      "    ~~~~^^",
      '  File "/private/tmp/app.py", line 2, in boom',
      '    raise ValueError("boom")',
      "ValueError: boom",
    ].join("\n");
    expect(stripStackTrace(traceback)).toBe("ValueError: boom");
    // The traceback is an Error's message, and the description is that Error's stack
    const relayed = new Error(traceback);
    expect(stripStackTrace(relayed.stack as string)).toBe("ValueError: boom");
    // ... or LangChain's run.error built from it
    expect(stripStackTrace(`${relayed.message}\n\n${relayed.stack}`)).toBe("ValueError: boom");
  });

  it("stripStackTrace keeps every exception line of a chained Python traceback", () => {
    const chained = [
      "Traceback (most recent call last):",
      '  File "/private/tmp/app.py", line 2, in <module>',
      "    d['x']",
      "KeyError: 'x'",
      "",
      "During handling of the above exception, another exception occurred:",
      "",
      "Traceback (most recent call last):",
      '  File "/private/tmp/app.py", line 4, in <module>',
      "    raise ValueError('boom')",
      "ValueError: boom",
      "with a second line",
    ].join("\n");
    expect(stripStackTrace(chained)).toBe("KeyError: 'x'\n\nValueError: boom\nwith a second line");
    expect(stripStackTrace(chained)).not.toContain("/private/tmp");
  });

  it("stripStackTrace keeps a multi-line message of a full err.stack", () => {
    const err = new Error("line one\nDETAILS");
    expect(stripStackTrace(err.stack as string)).toBe("Error: line one\nDETAILS");
    // LangChain's run.error repeats the message in the stack header; it is kept once
    expect(stripStackTrace(`${err.message}\n\n${err.stack}`)).toBe("line one\nDETAILS");
    const paragraphs = new Error("first\n\nsecond\nthird");
    expect(stripStackTrace(`${paragraphs.message}\n\n${paragraphs.stack}`)).toBe(
      "first\n\nsecond\nthird",
    );
    expect(stripStackTrace(paragraphs.stack as string)).toBe("Error: first\n\nsecond\nthird");
  });

  it("stripStackTrace leaves messages without a stack alone", () => {
    expect(stripStackTrace("Error: line one\nline two")).toBe("Error: line one\nline two");
    const prose = "Validation failed\n  at least one item is required";
    expect(stripStackTrace(prose)).toBe(prose);
  });

  it("stripStackTrace recognises the common frame shapes", () => {
    const stack = [
      "TypeError: bad input",
      "    at new Parser (<anonymous>)",
      "    at Array.map (native)",
      "    at async run (/private/tmp/app/index.js:10:5)",
      "    at /private/tmp/app/index.js:3:1",
    ].join("\n");
    expect(stripStackTrace(stack)).toBe("TypeError: bad input");
    expect(stripStackTrace(`bad input\n\n${stack}`)).toBe("bad input");
  });
});

describe("FISpan error-detail policy", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
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

  function tracerWith(traceConfig: TraceConfigOptions) {
    return new FITracer({ tracer: provider.getTracer("test"), traceConfig });
  }

  function onlySpan(): ReadableSpan {
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  function exceptionEvents(span: ReadableSpan) {
    return span.events.filter((event) => event.name === "exception");
  }

  /** The shape most instrumentors use: recordException then setStatus with the message */
  function failLikeAnInstrumentor(tracer: FITracer, error: Error) {
    const span = tracer.startSpan("llm call");
    span.recordException(error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
    span.end();
  }

  describe("default (hideErrorDetails off)", () => {
    it("keeps the message, the exception event and sets error.type", () => {
      const error = providerError();
      failLikeAnInstrumentor(tracerWith({}), error);
      const span = onlySpan();
      expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: error.message });
      expect(span.attributes[ERROR_TYPE]).toBe("model_not_found");
      const [event] = exceptionEvents(span);
      expect(event.attributes?.[EXCEPTION_TYPE]).toBe("model_not_found");
      expect(event.attributes?.[EXCEPTION_MESSAGE]).toBe(error.message);
      expect(event.attributes?.[EXCEPTION_STACKTRACE]).toBe(error.stack);
    });

    it("strips a stack trace from the status description", () => {
      const error = new Error("boom");
      const span = tracerWith({}).startSpan("s");
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: `${error.message}\n\n${error.stack}`,
      });
      span.end();
      const { status } = onlySpan();
      expect(status).toEqual({ code: SpanStatusCode.ERROR, message: "boom" });
      expect(status.message).not.toContain(" at ");
    });

    it("reduces a 'Name: message' description to the message", () => {
      const error = new TypeError("bad input");
      const span = tracerWith({}).startSpan("s");
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: String(error) });
      span.end();
      const finished = onlySpan();
      expect(finished.status).toEqual({ code: SpanStatusCode.ERROR, message: "bad input" });
      expect(finished.attributes[ERROR_TYPE]).toBe("TypeError");
    });

    it("a full err.stack description keeps the whole multi-line message", () => {
      const error = new Error("line one\nDETAILS");
      const span = tracerWith({}).startSpan("s");
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.stack });
      span.end();
      expect(onlySpan().status).toEqual({
        code: SpanStatusCode.ERROR,
        message: "line one\nDETAILS",
      });
    });

    it("a relayed Python traceback keeps its final 'Type: msg' line", () => {
      const error = new Error(
        'Traceback (most recent call last):\n  File "/private/tmp/app.py", line 1, in <module>\nValueError: boom',
      );
      failLikeAnInstrumentor(tracerWith({}), error);
      expect(onlySpan().status).toEqual({ code: SpanStatusCode.ERROR, message: "ValueError: boom" });
    });

    it("exception.type matches between the default and hidden modes", () => {
      const cases: [Error, string, string][] = [
        [new AnthropicNotFoundError("x"), "Error", "not_found_error"],
        // A numeric code (HTTP status) is not a type: the error class is used in both modes
        [Object.assign(new Error("x"), { code: 400 }), "Error", "Error"],
        [Object.assign(new NotFoundError("x"), { code: 404 }) as Error, "Error", "NotFoundError"],
        [Object.assign(new RangeError("x"), { code: 400 }), "RangeError", "RangeError"],
        [providerError(), "model_not_found", "model_not_found"],
      ];
      for (const [error, exceptionType, errorType] of cases) {
        for (const traceConfig of [{}, { hideInputs: true }]) {
          exporter.reset();
          failLikeAnInstrumentor(tracerWith(traceConfig), error);
          const span = onlySpan();
          expect(exceptionEvents(span)[0].attributes?.[EXCEPTION_TYPE]).toBe(exceptionType);
          expect(span.attributes[ERROR_TYPE]).toBe(errorType);
        }
      }
    });

    it("records a thrown string as the SDK does (message only)", () => {
      const span = tracerWith({}).startSpan("s");
      span.recordException("plain failure");
      span.end();
      expect(exceptionEvents(onlySpan())[0].attributes).toEqual({
        [EXCEPTION_MESSAGE]: "plain failure",
      });
    });

    it("keeps error detail attributes", () => {
      const span = tracerWith({}).startSpan("s");
      span.setAttribute(ERROR_MESSAGE, "kept");
      span.setAttributes({ [EXCEPTION_MESSAGE]: "kept too" });
      span.end();
      const { attributes } = onlySpan();
      expect(attributes[ERROR_MESSAGE]).toBe("kept");
      expect(attributes[EXCEPTION_MESSAGE]).toBe("kept too");
    });

    it("leaves non-error statuses and other events alone", () => {
      const span = tracerWith({}).startSpan("s");
      span.addEvent("custom", { note: "hello" });
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      const finished = onlySpan();
      expect(finished.status).toEqual({ code: SpanStatusCode.OK });
      expect(finished.attributes[ERROR_TYPE]).toBeUndefined();
      expect(finished.events[0].attributes).toEqual({ note: "hello" });
    });
  });

  describe.each([
    ["hideErrorDetails: true", { hideErrorDetails: true }],
    ["derived from hideInputs", { hideInputs: true }],
    ["derived from hideOutputText", { hideOutputText: true }],
  ])("hidden (%s)", (_label, traceConfig: TraceConfigOptions) => {
    it("record_exception keeps exception.type only and the status has no description", () => {
      failLikeAnInstrumentor(tracerWith(traceConfig), providerError());
      const span = onlySpan();
      expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
      expect(span.attributes[ERROR_TYPE]).toBe("model_not_found");
      const events = exceptionEvents(span);
      expect(events).toHaveLength(1);
      expect(events[0].attributes).toEqual({ [EXCEPTION_TYPE]: "model_not_found" });
      expect(
        JSON.stringify({ status: span.status, attributes: span.attributes, events: span.events }),
      ).not.toContain(PROMPT);
    });

    it("set_status with a description drops the description", () => {
      const span = tracerWith(traceConfig).startSpan("s");
      span.setStatus({ code: SpanStatusCode.ERROR, message: PROMPT });
      span.end();
      const finished = onlySpan();
      expect(finished.status).toEqual({ code: SpanStatusCode.ERROR });
      expect(finished.attributes[ERROR_TYPE]).toBe("_OTHER");
    });

    it("add_event('exception') drops the message and stack trace and keeps the rest", () => {
      const span = tracerWith(traceConfig).startSpan("s");
      span.addEvent("exception", {
        [EXCEPTION_TYPE]: "ValueError",
        [EXCEPTION_MESSAGE]: PROMPT,
        [EXCEPTION_STACKTRACE]: "Traceback ... /private/tmp/x.py",
        [EXCEPTION_ESCAPED]: true,
        [ERROR_MESSAGE]: PROMPT,
        "provider.error.message": PROMPT,
        "retry.attempt": 2,
        "app.component": "planner",
      });
      span.addEvent("exception", [1700000000, 0]);
      span.addEvent("not-an-exception", { note: "kept", [ERROR_MESSAGE]: PROMPT });
      span.setStatus({ code: SpanStatusCode.ERROR, message: PROMPT });
      span.end();
      const finished = onlySpan();
      const [first, second] = exceptionEvents(finished);
      // A denylist, as Python's strip_error_details: custom attributes are kept
      expect(first.attributes).toEqual({
        [EXCEPTION_TYPE]: "ValueError",
        [EXCEPTION_ESCAPED]: true,
        "retry.attempt": 2,
        "app.component": "planner",
      });
      expect(second.attributes).toEqual({});
      expect(second.time).toEqual([1700000000, 0]);
      expect(JSON.stringify(finished.events)).not.toContain(PROMPT);
      // Other events keep their attributes, minus error details
      expect(finished.events.find((e) => e.name === "not-an-exception")?.attributes).toEqual({
        note: "kept",
      });
      // error.type comes from the exception event
      expect(finished.attributes[ERROR_TYPE]).toBe("ValueError");
      expect(finished.status).toEqual({ code: SpanStatusCode.ERROR });
    });

    it("record_exception of a thrown string keeps a type-only event", () => {
      const span = tracerWith(traceConfig).startSpan("s");
      span.recordException(PROMPT);
      span.end();
      expect(exceptionEvents(onlySpan())[0].attributes).toEqual({ [EXCEPTION_TYPE]: "_OTHER" });
    });

    it("set_attribute(s) drops exception.message, exception.stacktrace and error.message", () => {
      const span = tracerWith(traceConfig).startSpan("s");
      span.setAttribute(ERROR_MESSAGE, PROMPT);
      span.setAttribute(EXCEPTION_STACKTRACE, "at /private/tmp/x.js:1:1");
      span.setAttributes({
        [EXCEPTION_MESSAGE]: PROMPT,
        [ERROR_TYPE]: "RateLimitError",
        "gen_ai.request.model": "gpt-x",
        // instrumentor-specific error text keys
        "autogen.error.message": PROMPT,
        "autogen.tool.error_message": PROMPT,
      });
      span.setStatus({ code: SpanStatusCode.ERROR });
      span.end();
      const { attributes } = onlySpan();
      expect(attributes[ERROR_MESSAGE]).toBeUndefined();
      expect(attributes[EXCEPTION_MESSAGE]).toBeUndefined();
      expect(attributes[EXCEPTION_STACKTRACE]).toBeUndefined();
      expect(attributes["autogen.error.message"]).toBeUndefined();
      expect(attributes["autogen.tool.error_message"]).toBeUndefined();
      // An error.type set by the instrumentation is kept
      expect(attributes[ERROR_TYPE]).toBe("RateLimitError");
      expect(attributes["gen_ai.request.model"]).toBe("gpt-x");
    });

    it("exceptions recorded through the active span of startActiveSpan are filtered too", async () => {
      const tracer = tracerWith(traceConfig);
      await tracer.startActiveSpan("outer", async (span) => {
        const active = trace.getActiveSpan();
        expect(active).toBe(span);
        await Promise.resolve();
        // e.g. user code or a nested helper reaching the span through the context
        trace.getActiveSpan()?.recordException(providerError());
        trace.getActiveSpan()?.setStatus({ code: SpanStatusCode.ERROR, message: PROMPT });
        span.end();
      });
      const finished = onlySpan();
      expect(finished.status).toEqual({ code: SpanStatusCode.ERROR });
      expect(exceptionEvents(finished)[0].attributes).toEqual({
        [EXCEPTION_TYPE]: "model_not_found",
      });
      expect(JSON.stringify(finished.events)).not.toContain(PROMPT);
    });
  });

  it("startActiveSpan still parents child spans and returns the callback result", () => {
    const tracer = tracerWith({});
    const result = tracer.startActiveSpan("parent", (parent) => {
      const child = tracer.startSpan("child");
      child.end();
      parent.end();
      return 42;
    });
    expect(result).toBe(42);
    const spans = exporter.getFinishedSpans();
    const parent = spans.find((s) => s.name === "parent");
    const child = spans.find((s) => s.name === "child");
    expect(child?.parentSpanContext?.spanId).toBe(parent?.spanContext().spanId);
  });

  it("error.type is only added when the span ends in ERROR", () => {
    const span = tracerWith({}).startSpan("s");
    span.recordException(new TypeError("handled"));
    span.setStatus({ code: SpanStatusCode.ERROR, message: "retrying" });
    // The SDK keeps the last status; a retry that succeeded leaves no error.type
    span.setStatus({ code: SpanStatusCode.OK });
    span.end();
    const finished = onlySpan();
    expect(finished.status).toEqual({ code: SpanStatusCode.OK });
    expect(finished.attributes[ERROR_TYPE]).toBeUndefined();
  });
});
