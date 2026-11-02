// Copy this fixture beside an isolated package.json after installing the built
// tarballs with @langchain/core and a compatible @langchain/langgraph. No model
// or vendor calls. Usage: node langgraph.mjs manual-cjs|manual-esm
//
// LangGraph pauses (interrupt()) and hands off (Command.PARENT) by throwing
// control-flow signals through the chain callbacks. Those runs succeeded, so their
// spans must not be ERROR, carry exception events or leak the interrupt payload.
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const mode = process.argv[2] ?? "manual-cjs";
assert.ok(["manual-cjs", "manual-esm"].includes(mode));
const load = mode === "manual-esm" ? (name) => import(name) : async (name) => require(name);
const { LangChainInstrumentation } = await load("@traceai/langchain");
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } =
  await load("@opentelemetry/sdk-trace-base");
const { SpanStatusCode } = await load("@opentelemetry/api");

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
new LangChainInstrumentation({ tracerProvider: provider })
  .manuallyInstrument(await load("@langchain/core/callbacks/manager"));
const { StateGraph, Annotation, START, END, MemorySaver, interrupt, Command } =
  await load("@langchain/langgraph");
const { awaitAllCallbacks } = await load("@langchain/core/callbacks/promises");
const versions = {
  core: require("@langchain/core/package.json").version,
  langgraph: require("@langchain/langgraph/package.json").version,
};

const State = Annotation.Root({ n: Annotation({ reducer: (_, next) => next, default: () => 0 }) });
const payload = "synthetic-private-approval-question";
const exceptions = (span) => span.events.filter((event) => event.name === "exception");
const isInterruptMarked = (span) => span.attributes["langgraph.interrupt"] === true;
async function finished() {
  await awaitAllCallbacks();
  const spans = exporter.getFinishedSpans();
  exporter.reset();
  return spans;
}
function assertSucceeded(spans, label) {
  assert.ok(spans.length > 0, `${label}: no spans exported`);
  for (const span of spans) {
    assert.notEqual(span.status.code, SpanStatusCode.ERROR,
      `${label}: ${span.name} exported as ERROR (${span.status.message})`);
    assert.deepEqual(exceptions(span), [], `${label}: ${span.name} recorded an exception`);
    assert.ok(!JSON.stringify(span.status).includes(payload), `${label}: status leaks the interrupt payload`);
  }
}

try {
  // 1. Human-in-the-loop pause, then resume.
  const hitl = new StateGraph(State)
    .addNode("ask_human", async (state) => ({ n: state.n + (interrupt(payload) ? 1 : 0) }))
    .addEdge(START, "ask_human")
    .addEdge("ask_human", END)
    .compile({ checkpointer: new MemorySaver() });
  const thread = { configurable: { thread_id: "synthetic-thread" } };
  await hitl.invoke({ n: 0 }, thread);
  const pending = await hitl.getState(thread);
  assert.ok(pending.tasks.some((task) => task.interrupts?.length), "graph must pause on the interrupt");
  let spans = await finished();
  assertSucceeded(spans, "interrupt");
  const pausedNode = spans.filter((span) => span.name === "ask_human");
  assert.equal(pausedNode.length, 1, "one paused node span");
  assert.ok(isInterruptMarked(pausedNode[0]), "the paused node is marked langgraph.interrupt");
  assert.ok(pausedNode[0].events.some((event) => event.name === "interrupt"), "the paused node has an interrupt event");
  assert.deepEqual(spans.filter(isInterruptMarked).map((span) => span.name), ["ask_human"]);

  assert.deepEqual(await hitl.invoke(new Command({ resume: true }), thread), { n: 1 });
  spans = await finished();
  assertSucceeded(spans, "resume");
  assert.deepEqual(spans.filter(isInterruptMarked), [], "a resumed run is not an interrupt");

  // 2. Subgraph node hands off to its parent graph.
  const subgraph = new StateGraph(State)
    .addNode("route", async () => new Command({ graph: Command.PARENT, goto: "done", update: { n: 5 } }))
    .addEdge(START, "route")
    .compile();
  const parent = new StateGraph(State)
    .addNode("sub", subgraph, { ends: ["done"] })
    .addNode("done", async (state) => ({ n: state.n + 1 }))
    .addEdge(START, "sub")
    .addEdge("done", END)
    .compile();
  assert.deepEqual(await parent.invoke({ n: 0 }), { n: 6 });
  spans = await finished();
  assertSucceeded(spans, "handoff");
  for (const name of ["route", "sub", "done"]) {
    assert.ok(spans.some((span) => span.name === name), `handoff: missing ${name} span`);
  }
  assert.deepEqual(spans.filter(isInterruptMarked), [], "a handoff is not an interrupt");

  // 3. Negative control: a real node failure is still an error.
  const failure = new Error("synthetic node failure");
  const failing = new StateGraph(State)
    .addNode("broken", async () => { throw failure; })
    .addEdge(START, "broken")
    .addEdge("broken", END)
    .compile();
  await assert.rejects(failing.invoke({ n: 0 }), (error) => error.message === failure.message);
  spans = await finished();
  const broken = spans.filter((span) => span.name === "broken");
  assert.equal(broken.length, 1);
  assert.equal(broken[0].status.code, SpanStatusCode.ERROR);
  assert.equal(exceptions(broken[0]).length, 1);
  assert.ok(!isInterruptMarked(broken[0]));

  console.log(`PASS: LangGraph control flow (${mode}, core ${versions.core}, langgraph ${versions.langgraph})`);
} finally {
  await provider.shutdown();
}
