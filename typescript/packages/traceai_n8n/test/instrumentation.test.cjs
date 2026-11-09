const assert = require("node:assert/strict");
const { after, afterEach, beforeEach, test } = require("node:test");
const { spawnSync } = require("node:child_process");
const { join } = require("node:path");
const { pathToFileURL } = require("node:url");
const {
  context,
  trace,
  SpanStatusCode,
  ROOT_CONTEXT,
} = require("@opentelemetry/api");
const { suppressTracing } = require("@opentelemetry/core");
const {
  AsyncLocalStorageContextManager,
} = require("@opentelemetry/context-async-hooks");
const {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} = require("@opentelemetry/sdk-trace-base");
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const { fixture, cleanup, WorkflowExecute } = require("./workflow-fixture.cjs");
const { N8nInstrumentation } = require("../dist/src/index.js");

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
const manager = new AsyncLocalStorageContextManager().enable();
provider.register({ contextManager: manager });
const tracer = provider.getTracer("n8n-tests");
const originals = {
  workflow: WorkflowExecute.prototype.processRunExecutionData,
  node: WorkflowExecute.prototype.runNode,
};
let instrumentation;

beforeEach(() => {
  exporter.reset();
  instrumentation = new N8nInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument({ WorkflowExecute });
});
afterEach(() => instrumentation.disable());
after(async () => {
  manager.disable();
  await provider.shutdown();
  await cleanup();
});

test("runs real nodes, preserves output and parents nested application spans", async () => {
  const parent = tracer.startSpan("application");
  const f = fixture({
    execute: async function () {
      const child = tracer.startSpan("inside-node");
      child.end();
      return [[{ json: { value: this.getInputData()[0].json.value * 2 } }]];
    },
  });
  const active = trace.setSpan(context.active(), parent);
  const result = await context.with(active, () => {
    const promise = f.run();
    assert.equal(trace.getSpan(context.active()), parent);
    assert.equal(typeof promise.cancel, "function");
    return promise;
  });
  parent.end();
  assert.equal(result.status, "success");
  assert.deepEqual(
    result.data.resultData.runData.Transform[0].data.main[0][0].json,
    { value: 42 },
  );
  const spans = exporter.getFinishedSpans();
  const workflow = spans.find((s) => s.name.startsWith("n8n workflow:"));
  const nodes = spans.filter((s) => s.name.startsWith("n8n node:"));
  assert.equal(nodes.length, 2);
  assert.equal(workflow.parentSpanContext.spanId, parent.spanContext().spanId);
  assert.equal(workflow.attributes["n8n.execution.id"], "execution-workflow-1");
  assert.equal(workflow.attributes["n8n.execution.status"], "success");
  assert.equal(workflow.status.code, SpanStatusCode.OK);
  for (const node of nodes) {
    assert.equal(node.parentSpanContext.spanId, workflow.spanContext().spanId);
    assert.equal(node.spanContext().traceId, workflow.spanContext().traceId);
  }
  const transform = nodes.find(
    (s) => s.attributes["n8n.node.name"] === "Transform",
  );
  assert.equal(
    spans.find((s) => s.name === "inside-node").parentSpanContext.spanId,
    transform.spanContext().spanId,
  );
  assert.equal(trace.getSpan(context.active()), undefined);
});

test("records engine-reported failures without exporting error payloads", async () => {
  const error = new Error("secret-from-node-payload");
  const result = await fixture({
    execute: async () => {
      throw error;
    },
  }).run();
  assert.equal(result.status, "error");
  assert.match(
    result.data.resultData.error.message,
    /secret-from-node-payload/,
  );
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 3);
  assert.equal(
    spans.find((s) => s.name === "n8n node: Transform").status.code,
    SpanStatusCode.ERROR,
  );
  assert.equal(
    spans.find((s) => s.name.startsWith("n8n workflow:")).status.code,
    SpanStatusCode.ERROR,
  );
  assert.ok(
    !JSON.stringify(
      spans.map((s) => ({
        attributes: s.attributes,
        events: s.events,
        status: s.status,
      })),
    ).includes(error.message),
  );
});

test("exports metadata without reading node parameters or execution items", async () => {
  await fixture().run();
  const spans = exporter.getFinishedSpans();
  assert.ok(
    !JSON.stringify(spans.map((s) => s.attributes)).includes("not-for-tracing"),
  );
  for (const span of spans) {
    assert.ok(!Object.hasOwn(span.attributes, "input.value"));
    assert.ok(!Object.hasOwn(span.attributes, "output.value"));
  }
});

test("cancel closes active spans once and keeps the engine's cancellation result", async () => {
  let release;
  let started;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  const f = fixture({
    execute: async () => {
      started();
      await pending;
      return [[{ json: { done: true } }]];
    },
  });
  const promise = f.run();
  await ready;
  assert.equal(trace.getSpan(context.active()), undefined);
  promise.cancel();
  assert.equal(promise.isCanceled, true);
  const canceledSpans = exporter.getFinishedSpans();
  assert.equal(canceledSpans.length, 3);
  assert.equal(
    canceledSpans.find((s) => s.name.startsWith("n8n workflow:")).attributes[
      "n8n.execution.status"
    ],
    "canceled",
  );
  assert.equal(
    canceledSpans.find((s) => s.name === "n8n node: Transform").attributes[
      "n8n.node.status"
    ],
    "canceled",
  );
  release();
  const result = await promise;
  assert.equal(result.status, "canceled");
  assert.equal(exporter.getFinishedSpans().length, 3);
});

test("isolates two interleaved workflow executions", async () => {
  const releases = [];
  const ready = [];
  const tasks = ["one", "two"].map((id) => {
    let started;
    ready.push(
      new Promise((resolve) => {
        started = resolve;
      }),
    );
    const pending = new Promise((resolve) => {
      releases.push(resolve);
    });
    return fixture({
      id,
      execute: async () => {
        started();
        await pending;
        return [[{ json: { id } }]];
      },
    }).run();
  });
  await Promise.all(ready);
  assert.equal(trace.getSpan(context.active()), undefined);
  releases[1]();
  releases[0]();
  const results = await Promise.all(tasks);
  assert.deepEqual(
    results.map((r) => r.status),
    ["success", "success"],
  );
  const spans = exporter.getFinishedSpans();
  const roots = spans.filter((s) => s.name.startsWith("n8n workflow:"));
  assert.equal(roots.length, 2);
  assert.notEqual(
    roots[0].spanContext().traceId,
    roots[1].spanContext().traceId,
  );
  for (const root of roots) {
    const nodes = spans.filter(
      (s) => s.parentSpanContext?.spanId === root.spanContext().spanId,
    );
    assert.equal(nodes.length, 2);
    assert.ok(
      nodes.every(
        (n) =>
          n.attributes["n8n.execution.id"] ===
          root.attributes["n8n.execution.id"],
      ),
    );
  }
});

test("honors OpenTelemetry suppression", async () => {
  const result = await context.with(suppressTracing(ROOT_CONTEXT), () =>
    fixture().run(),
  );
  assert.equal(result.status, "success");
  assert.equal(exporter.getFinishedSpans().length, 0);
});

test("manual instrumentation is idempotent and disable restores original methods", async () => {
  const wrapped = WorkflowExecute.prototype.processRunExecutionData;
  instrumentation.manuallyInstrument({ WorkflowExecute });
  assert.equal(WorkflowExecute.prototype.processRunExecutionData, wrapped);
  instrumentation.disable();
  assert.equal(
    WorkflowExecute.prototype.processRunExecutionData,
    originals.workflow,
  );
  assert.equal(WorkflowExecute.prototype.runNode, originals.node);
  await fixture().run();
  assert.equal(exporter.getFinishedSpans().length, 0);
  instrumentation.enable();
  instrumentation.manuallyInstrument({ WorkflowExecute });
  await fixture().run();
  assert.equal(exporter.getFinishedSpans().length, 3);
});

test("preserves synchronous engine validation errors and restores caller context", () => {
  const f = fixture();
  f.workflow.nodes.Start.type = "missing.node";
  assert.throws(() => f.run());
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  assert.equal(spans[0].status.code, SpanStatusCode.ERROR);
  assert.equal(trace.getSpan(context.active()), undefined);
});

test("returns the exact PCancelable created by the engine and restores cancel", async () => {
  instrumentation.disable();
  let originalPromise;
  class ObservedEngine extends WorkflowExecute {
    processRunExecutionData(workflow) {
      originalPromise = originals.workflow.call(this, workflow);
      return originalPromise;
    }
  }
  instrumentation.enable();
  instrumentation.manuallyInstrument({ WorkflowExecute: ObservedEngine });
  const promise = fixture({ Engine: ObservedEngine }).run();
  assert.equal(promise, originalPromise);
  await promise;
  assert.equal(Object.hasOwn(promise, "cancel"), false);
  assert.equal(
    exporter
      .getFinishedSpans()
      .filter((s) => s.name.startsWith("n8n workflow:")).length,
    1,
  );
});

test("keeps a continue-on-fail workflow successful while marking the failed node", async () => {
  const f = fixture({
    afterTransform: true,
    execute: async () => {
      throw new Error("local fixture failure");
    },
  });
  f.workflow.nodes.Transform.continueOnFail = true;
  const result = await f.run();
  assert.equal(result.status, "success");
  const spans = exporter.getFinishedSpans();
  assert.equal(
    spans.find((s) => s.name.startsWith("n8n workflow:")).status.code,
    SpanStatusCode.OK,
  );
  assert.equal(
    spans.find((s) => s.name === "n8n node: Transform").status.code,
    SpanStatusCode.ERROR,
  );
});

test("records each retry attempt without changing the final workflow status", async () => {
  let attempts = 0;
  const f = fixture({
    execute: async function () {
      if (++attempts === 1) throw new Error("transient fixture failure");
      return [this.getInputData()];
    },
  });
  Object.assign(f.workflow.nodes.Transform, {
    retryOnFail: true,
    maxTries: 2,
    waitBetweenTries: 1,
  });
  const result = await f.run();
  assert.equal(result.status, "success");
  assert.equal(attempts, 2);
  const spans = exporter.getFinishedSpans();
  const retries = spans.filter((s) => s.name === "n8n node: Transform");
  assert.deepEqual(
    retries.map((s) => s.status.code),
    [SpanStatusCode.ERROR, SpanStatusCode.OK],
  );
  const workflow = spans.find((s) => s.name.startsWith("n8n workflow:"));
  assert.equal(workflow.status.code, SpanStatusCode.OK);
  assert.ok(
    retries.every(
      (s) => s.parentSpanContext.spanId === workflow.spanContext().spanId,
    ),
  );
});

test("disabled construction does not patch an already-loaded engine", async () => {
  instrumentation.disable();
  const disabled = new N8nInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  disabled.manuallyInstrument({ WorkflowExecute });
  assert.equal(
    WorkflowExecute.prototype.processRunExecutionData,
    originals.workflow,
  );
  const result = await fixture().run();
  assert.equal(result.status, "success");
  assert.equal(exporter.getFinishedSpans().length, 0);
  disabled.disable();
});

test("finishes an in-flight execution after disabling instrumentation", async () => {
  let started;
  let release;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  const promise = fixture({
    execute: async function () {
      started();
      await pending;
      return [this.getInputData()];
    },
  }).run();
  await ready;
  instrumentation.disable();
  assert.equal(
    WorkflowExecute.prototype.processRunExecutionData,
    originals.workflow,
  );
  assert.equal(WorkflowExecute.prototype.runNode, originals.node);
  release();
  const result = await promise;
  assert.equal(result.status, "success");
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 3);
  assert.ok(spans.every((s) => s.status.code === SpanStatusCode.OK));
  assert.equal(Object.hasOwn(promise, "cancel"), false);
});

test("automatically patches n8n-core loaded after instrumentation in a fresh process", () => {
  const child = spawnSync(
    process.execPath,
    [join(__dirname, "auto-instrument.cjs")],
    {
      encoding: "utf8",
      timeout: 20000,
      env: { ...process.env, N8N_DIAGNOSTICS_ENABLED: "false" },
    },
  );
  assert.equal(
    child.status,
    0,
    child.stdout + child.stderr + String(child.error ?? ""),
  );
  const result = JSON.parse(child.stdout.trim().split("\n").at(-1));
  assert.equal(result.status, "success");
  assert.equal(result.nodes, 2);
  assert.equal(result.workflows, 1);
  assert.equal(result.restored, true);
});

test("loads the ESM entry point", async () => {
  const esm = await import(
    pathToFileURL(join(__dirname, "../dist/esm/index.js")).href
  );
  assert.equal(typeof esm.N8nInstrumentation, "function");
  const instance = new esm.N8nInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  instance.disable();
});
