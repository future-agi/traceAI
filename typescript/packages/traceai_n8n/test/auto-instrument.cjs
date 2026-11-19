const {
  AsyncLocalStorageContextManager,
} = require("@opentelemetry/context-async-hooks");
const {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} = require("@opentelemetry/sdk-trace-base");
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const { isWrapped } = require("@opentelemetry/instrumentation");
const { N8nInstrumentation } = require("../dist/src/index.js");

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
const manager = new AsyncLocalStorageContextManager().enable();
provider.register({ contextManager: manager });
const instrumentation = new N8nInstrumentation();
instrumentation.setTracerProvider(provider);

// This is the first n8n-core import in this process; no manual patch call.
const { fixture, cleanup, WorkflowExecute } = require("./workflow-fixture.cjs");

(async () => {
  try {
    const result = await fixture().run();
    const spans = exporter.getFinishedSpans();
    instrumentation.disable();
    console.log(
      JSON.stringify({
        status: result.status,
        workflows: spans.filter((s) => s.name.startsWith("n8n workflow:"))
          .length,
        nodes: spans.filter((s) => s.name.startsWith("n8n node:")).length,
        restored:
          !isWrapped(WorkflowExecute.prototype.processRunExecutionData) &&
          !isWrapped(WorkflowExecute.prototype.runNode),
      }),
    );
  } finally {
    instrumentation.disable();
    await provider.shutdown();
    manager.disable();
    await cleanup();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
