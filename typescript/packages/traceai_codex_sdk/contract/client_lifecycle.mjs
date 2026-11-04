// Real SDK factories, offline fake subprocess, independent tracer providers.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { Codex } from "@openai/codex-sdk";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CodexSDKInstrumentation, isPatched } from "../dist/esm/index.js";

const methods = ["startThread", "resumeThread"];
const client = () => new Codex({
  codexPathOverride: fileURLToPath(new URL("./fake-codex.mjs", import.meta.url)),
  env: { PATH: process.env.PATH ?? "", FAKE_CODEX_SCENARIO: "tool" },
});
const memory = () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
  return { exporter, provider, instrumentation };
};
const thread = (codex, method) => method === "startThread"
  ? codex.startThread({ skipGitRepoCheck: true })
  : codex.resumeThread("thread-lifecycle", { skipGitRepoCheck: true });
const factories = (codex) => methods.map((method) => codex[method]);

for (const order of ["registration", "reverse"]) {
  const a = memory(), b = memory(), codex = client();
  const original = factories(codex);
  try {
    a.instrumentation.wrapCodex(codex);
    const active = factories(codex);
    a.instrumentation.wrapCodex(codex);
    assert.deepEqual(factories(codex), active, "same-owner wrapping must not stack");
    b.instrumentation.wrapCodex(codex);
    assert.deepEqual(factories(codex), active, "different-owner wrapping must not stack");
    for (const method of methods) await thread(codex, method).run("before cleanup");
    assert.equal(a.exporter.getFinishedSpans().filter((s) => s.name === "codex.turn").length, 2);
    assert.equal(b.exporter.getFinishedSpans().length, 0);
    a.exporter.reset();
    for (const owner of order === "registration" ? [a, b] : [b, a]) owner.instrumentation.unpatch();
    assert.deepEqual(factories(codex), original);
    for (const method of methods) {
      assert.equal(Object.hasOwn(codex, method), false);
      const next = thread(codex, method);
      assert.equal(isPatched(next), false);
      await next.run("after cleanup");
    }
    assert.equal(a.exporter.getFinishedSpans().length, 0);
    assert.equal(b.exporter.getFinishedSpans().length, 0);
    // A new owner can subsequently trace, and repeated old cleanup is harmless.
    b.instrumentation.wrapCodex(codex);
    a.instrumentation.unpatch();
    await codex.startThread({ skipGitRepoCheck: true }).run("fresh owner");
    assert.equal(b.exporter.getFinishedSpans().filter((s) => s.name === "codex.turn").length, 1);
    console.log(`PASS real SDK client lifecycle: duplicate wrapping, ${order} cleanup, fresh owner`);
  } finally {
    a.instrumentation.unpatch();
    b.instrumentation.unpatch();
    await a.provider.shutdown();
    await b.provider.shutdown();
  }
}

for (const method of methods) {
  const a = memory(), codex = client();
  const original = codex[method];
  const descriptor = { value: original.bind(codex), writable: false, enumerable: true, configurable: true };
  try {
    Object.defineProperty(codex, method, descriptor);
    a.instrumentation.wrapCodex(codex);
    await thread(codex, method).run("bound own factory");
    a.instrumentation.unpatch();
    assert.deepEqual(Object.getOwnPropertyDescriptor(codex, method), descriptor);
    assert.equal(a.exporter.getFinishedSpans().filter((s) => s.name === "codex.turn").length, 1);
    a.exporter.reset();
    a.instrumentation.wrapCodex(codex);
    const saved = codex[method];
    const replacement = { value: () => "application replacement", writable: false, enumerable: true, configurable: true };
    Object.defineProperty(codex, method, replacement);
    a.instrumentation.unpatch();
    assert.deepEqual(Object.getOwnPropertyDescriptor(codex, method), replacement);
    // An application may retain our wrapper; cleanup must make it permanently inert.
    a.instrumentation.wrapCodex(client());
    const next = method === "startThread" ? saved.call(codex) : saved.call(codex, "thread-lifecycle");
    assert.equal(isPatched(next), false);
    await next.run("saved inactive factory");
    assert.equal(a.exporter.getFinishedSpans().length, 0);
    console.log(`PASS real SDK ${method}: bound descriptor, application replacement, inert saved wrapper`);
  } finally {
    a.instrumentation.unpatch();
    await a.provider.shutdown();
  }
}
