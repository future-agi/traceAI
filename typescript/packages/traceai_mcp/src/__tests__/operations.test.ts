import {
  context,
  trace,
  ROOT_CONTEXT,
  SpanStatusCode,
} from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { Operations, FastMCPOptions, transportOperations } from "../operations";
import { MCPInstrumentation } from "../mcp";

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
const tracer = provider.getTracer("contract");
const manager = new AsyncLocalStorageContextManager();
const request = (id: number, args: unknown = {}) => ({
  id,
  method: "tools/call",
  params: { name: "tool", arguments: args },
});
const response = (id: number, result: unknown = {}) => ({ id, result });
const tick = () => new Promise<void>((r) => setImmediate(r));
let operations: Operations;
const setup = (options: Partial<FastMCPOptions> = {}) =>
  new Operations(tracer, { version: "4.23.8", ...options }, () => undefined);
beforeAll(() => context.setGlobalContextManager(manager.enable()));
beforeEach(() => {
  exporter.reset();
  operations = setup();
});
afterEach(() => {
  operations.close();
  jest.useRealTimers();
});
afterAll(async () => {
  await provider.shutdown();
  manager.disable();
  context.disable();
});

it("does not end on dispatch, ends once after send, and preserves promise identity", async () => {
  let resolve!: () => void;
  const sent = new Promise<void>((r) => (resolve = r));
  const dispatchReturn = {};
  expect(operations.receive(request(1), () => dispatchReturn)).toBe(
    dispatchReturn,
  );
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  expect(operations.send(response(1), () => sent)).toBe(sent);
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  resolve();
  await tick();
  operations.close();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.OK);
});

it.each(["dispatch", "send", "async-send"])(
  "preserves %s failures without exporting exception details",
  async (kind) => {
    const error = new Error("PRIVATE_ERROR_CANARY");
    if (kind === "dispatch")
      expect(() =>
        operations.receive(request(1), () => {
          throw error;
        }),
      ).toThrow(error);
    else {
      operations.receive(request(1), () => {});
      if (kind === "send")
        expect(() =>
          operations.send(response(1), () => {
            throw error;
          }),
        ).toThrow(error);
      else {
        const rejected = Promise.reject(error);
        expect(operations.send(response(1), () => rejected)).toBe(rejected);
        await expect(rejected).rejects.toBe(error);
        await tick();
      }
    }
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(
      SpanStatusCode.ERROR,
    );
    expect(exporter.getFinishedSpans()[0].events).toEqual([]);
    expect(
      JSON.stringify(exporter.getFinishedSpans()[0].attributes),
    ).not.toContain("PRIVATE");
    expect(exporter.getFinishedSpans()[0].status.message).toBeUndefined();
  },
);

it("bounds pending requests and age without changing dispatch or cancelling work", async () => {
  jest.useFakeTimers({ doNotFake: ["nextTick", "queueMicrotask"] });
  const scheduled = jest.spyOn(global, "setTimeout");
  const cleared = jest.spyOn(global, "clearTimeout");
  operations = setup({ maxPendingRequests: 2, maxDurationMs: 100 });
  const dispatch = jest.fn();
  for (let id = 1; id <= 100; id++) operations.receive(request(id), dispatch);
  expect(dispatch).toHaveBeenCalledTimes(100);
  jest.advanceTimersByTime(100);
  expect(exporter.getFinishedSpans()).toHaveLength(2);
  expect(
    exporter
      .getFinishedSpans()
      .every((s) => s.attributes["error.type"] === "deadline_exceeded"),
  ).toBe(true);
  operations.receive(request(200), dispatch);
  operations.close();
  expect(exporter.getFinishedSpans()).toHaveLength(3);
  const operationTimers = scheduled.mock.calls.flatMap((args, index) =>
    args[1] === 100 ? [scheduled.mock.results[index].value] : [],
  );
  expect(operationTimers).toHaveLength(3);
  for (const timer of operationTimers)
    expect(cleared).toHaveBeenCalledWith(timer);
  // Flush the exporter's separate zero-delay callbacks, not part of operation state.
  jest.runOnlyPendingTimers();
  await provider.forceFlush();
  scheduled.mockRestore();
  cleared.mockRestore();
});

it("cancellation and close end once, notifications and responses create no spans", async () => {
  operations.receive(request(1), () => {});
  operations.receive(
    {
      method: "notifications/cancelled",
      params: { requestId: 1, reason: "PRIVATE" },
    },
    () => {},
  );
  operations.receive(
    { method: "notifications/progress", params: { content: "PRIVATE" } },
    () => {},
  );
  operations.receive(response(4), () => {});
  operations.send(response(1), () => Promise.resolve());
  await tick();
  operations.close();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes["error.type"]).toBe(
    "cancelled",
  );
});

it("does not replace duplicate pending IDs or end a reused ID on an old send completion", async () => {
  let finishOld!: () => void;
  operations.receive(request(1), () => {});
  operations.receive(request(1), () => {});
  operations.send(response(1), () => new Promise<void>((r) => (finishOld = r)));
  operations.receive(
    { method: "notifications/cancelled", params: { requestId: 1 } },
    () => {},
  );
  operations.receive(request(1), () => {});
  finishOld();
  await tick();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  operations.send(response(1), () => undefined);
  await tick();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

it("maintains async active context for overlapping operations and restores the caller", async () => {
  const work: Promise<void>[] = [];
  const children: string[] = [];
  for (let id = 1; id <= 2; id++)
    context.with(ROOT_CONTEXT, () =>
      operations.receive(request(id), () => {
        work.push(
          (async () => {
            const parent = trace.getSpanContext(context.active())!.spanId;
            await tick();
            expect(trace.getSpanContext(context.active())!.spanId).toBe(parent);
            children.push(parent);
          })(),
        );
      }),
    );
  await Promise.all(work);
  expect(new Set(children).size).toBe(2);
  expect(trace.getSpan(context.active())).toBeUndefined();
});

it("default captures no arguments, output, structured content or errors", async () => {
  operations.receive(request(1, { value: "PRIVATE_INPUT" }), () => {});
  operations.send(
    response(1, {
      content: [{ type: "text", text: "PRIVATE_OUTPUT" }],
      structuredContent: { secret: "PRIVATE_STRUCTURE" },
    }),
    () => undefined,
  );
  await tick();
  const span = exporter.getFinishedSpans()[0];
  expect(JSON.stringify(span.attributes)).not.toContain("PRIVATE");
  expect(span.events).toEqual([]);
});

it("explicit capture rejects binary, credentials, getters, cycles and oversized JSON", async () => {
  operations = setup({
    captureContent: { input: true, output: true, maxLength: 256 },
  });
  const getter = jest.fn(() => "PRIVATE_GETTER");
  const args = {
    value: "allowed",
    _meta: { secret: "PRIVATE_META" },
    password: "PRIVATE_PASSWORD",
    array: new Array(1),
  };
  Object.defineProperty(args.array, "0", { get: getter, enumerable: true });
  Object.defineProperty(args, "getter", { get: getter, enumerable: true });
  operations.receive(request(1, args), () => {});
  operations.send(
    response(1, {
      content: [
        { type: "audio", data: "PRIVATE_BINARY" },
        { type: "text", text: "allowed" },
      ],
      structuredContent: { authToken: "PRIVATE_TOKEN" },
    }),
    () => undefined,
  );
  await tick();
  expect(getter).not.toHaveBeenCalled();
  const attributes = exporter.getFinishedSpans()[0].attributes;
  expect(attributes["input.value"]).toContain("allowed");
  expect(attributes["output.value"]).toContain("allowed");
  expect(JSON.stringify(attributes)).not.toContain("PRIVATE");
  const circular: Record<string, unknown> = {};
  circular.self = circular;
  operations.receive(request(2, circular), () => {});
  operations.send(
    response(2, { content: "large".repeat(5000) }),
    () => undefined,
  );
  await tick();
  expect(
    exporter.getFinishedSpans()[1].attributes["output.value"],
  ).toBeUndefined();
});

it.each([
  "FI_HIDE_INPUTS",
  "FI_HIDE_INPUT_TEXT",
  "FI_HIDE_INPUT_MESSAGES",
  "FI_HIDE_OUTPUTS",
  "FI_HIDE_OUTPUT_TEXT",
  "FI_HIDE_OUTPUT_MESSAGES",
])("honours %s with explicit capture", async (key) => {
  process.env[key] = " TRUE ";
  try {
    operations = setup({ captureContent: { input: true, output: true } });
    operations.receive(request(1, { value: "allowed" }), () => {});
    operations.send(response(1, { value: "allowed" }), () => undefined);
    await tick();
    expect(
      exporter.getFinishedSpans()[0].attributes[
        key.includes("INPUT") ? "input.value" : "output.value"
      ],
    ).toBeUndefined();
  } finally {
    delete process.env[key];
  }
});

it("discovery remains metadata-only even when content is enabled", async () => {
  operations = setup({ captureContent: { input: true, output: true } });
  operations.receive(
    { id: 1, method: "tools/list", params: { arguments: "PRIVATE" } },
    () => {},
  );
  operations.send(
    response(1, { tools: [{ description: "PRIVATE" }] }),
    () => undefined,
  );
  await tick();
  expect(
    JSON.stringify(exporter.getFinishedSpans()[0].attributes),
  ).not.toContain("PRIVATE");
});

it("version gating and unsupported shapes are safe, without loading fastmcp", () => {
  const inst = new MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  class Session {
    get server() {
      return { connect() {}, close() {} };
    }
    connect() {}
  }
  const module = { FastMCPSession: Session };
  const original = Session.prototype.connect;
  for (const version of ["3.34.0", "3.36.0", "5.0.0", "4.0.0-beta.1", "bad"]) {
    expect(inst.instrumentFastMCP(module, { version })).toBe(false);
    expect(Session.prototype.connect).toBe(original);
  }
  expect(inst.instrumentFastMCP({} as never, { version: "4.23.8" })).toBe(
    false,
  );
  Object.freeze(Session.prototype);
  expect(inst.instrumentFastMCP(module, { version: "4.23.8" })).toBe(false);
  inst.disable();
});

it("restores multiple module copies, frozen messages, receivers and connect failures", async () => {
  const inst = new MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  inst.setTracerProvider(provider);
  for (let copy = 0; copy < 2; copy++) {
    class Session {
      get server() {
        return { connect() {}, close() {} };
      }
      async connect(transport: typeof wire) {
        transport.onmessage = (message: unknown) => {
          expect(message).toBe(frozen);
          return "return-value";
        };
        await transport.start();
      }
    }
    const frozen = Object.freeze(request(1));
    const wire = {
      async start() {},
      async send() {},
      async close() {},
      onmessage: (_: unknown): unknown => undefined,
    };
    const original = Session.prototype.connect;
    expect(
      inst.instrumentFastMCP(
        { FastMCPSession: Session },
        { version: "4.23.8" },
      ),
    ).toBe(true);
    await new Session().connect(wire);
    expect(wire.onmessage(frozen)).toBe("return-value");
    await wire.close();
    expect(transportOperations.has(wire)).toBe(false);
    inst.disable();
    expect(Session.prototype.connect).toBe(original);
  }
  class BrokenSession {
    get server() {
      return { connect() {}, close() {} };
    }
    connect() {
      return Promise.reject(new Error("synthetic"));
    }
  }
  const wire = { async start() {}, async send() {}, async close() {} };
  inst.instrumentFastMCP(
    { FastMCPSession: BrokenSession },
    { version: "4.23.8" },
  );
  await expect(
    Reflect.apply(BrokenSession.prototype.connect, new BrokenSession(), [wire]),
  ).rejects.toThrow("synthetic");
  expect(transportOperations.has(wire)).toBe(false);
  inst.disable();
});
