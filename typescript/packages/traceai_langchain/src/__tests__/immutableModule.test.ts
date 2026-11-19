import { describe, it, expect, beforeEach, afterEach } from "@jest/globals";
import { types } from "util";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { RunnableLambda } from "@langchain/core/runnables";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import { isWrapped } from "@opentelemetry/instrumentation";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { LangChainInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";

type DiagEntry = { level: string; message: string; args: unknown[] };

// Stands in for an ES module namespace (or a bundler's frozen exports): the
// object cannot gain properties, so assigning one throws in strict-mode code.
function moduleNamespace<T extends object>(exports: T): T {
  const namespace: Record<PropertyKey, unknown> = Object.create(null);
  for (const key of Object.keys(exports)) namespace[key] = (exports as Record<string, unknown>)[key];
  Object.defineProperty(namespace, Symbol.toStringTag, { value: "Module" });
  return Object.freeze(namespace) as T;
}

function captureDiag(): DiagEntry[] {
  const entries: DiagEntry[] = [];
  const record = (level: string) => (message: string, ...args: unknown[]) => {
    entries.push({ level, message, args });
  };
  diag.disable();
  diag.setLogger({
    error: record("error"),
    warn: record("warn"),
    info: record("info"),
    debug: record("debug"),
    verbose: record("verbose"),
  }, DiagLogLevel.ALL);
  return entries;
}

function problems(entries: DiagEntry[]) {
  return entries
    .filter(({ level, args }) => level === "warn" || level === "error" || args.some((arg) => types.isNativeError(arg)))
    .map(({ level, message, args }) => `${level}: ${message} ${args.map(String).join(" ")}`);
}

describe("instrumenting an immutable module namespace", () => {
  // Newer @langchain/core wraps _configureSync; the 0.2.0 floor only has configure.
  const configureSync = () => {
    const manager = CallbackManagerModule.CallbackManager as unknown as Record<string, unknown>;
    return ("_configureSync" in manager ? manager._configureSync : manager.configure) as unknown as { __original?: unknown };
  };
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: LangChainInstrumentation;
  let namespace: typeof CallbackManagerModule;
  let logged: DiagEntry[];

  beforeEach(() => {
    _resetPatchedStateForTesting();
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new LangChainInstrumentation({ tracerProvider: provider });
    namespace = moduleNamespace(CallbackManagerModule);
    logged = captureDiag();
  });

  afterEach(async () => {
    instrumentation.disable();
    diag.disable();
    await provider.shutdown();
  });

  it("uses a namespace that rejects new properties", () => {
    expect(Object.prototype.toString.call(namespace)).toBe("[object Module]");
    expect(() => {
      (namespace as unknown as Record<string, unknown>).fiPatched = true;
    }).toThrow(TypeError);
  });

  it("patches and unpatches without throwing or logging failures", () => {
    expect(() => instrumentation.manuallyInstrument(namespace)).not.toThrow();
    expect(isPatched()).toBe(true);
    expect(isWrapped(configureSync())).toBe(true);

    expect(() => instrumentation.disable()).not.toThrow();
    expect(isPatched()).toBe(false);
    expect(isWrapped(configureSync())).toBe(false);
    expect(problems(logged)).toEqual([]);
  });

  it("wraps the callback manager once when instrumented twice", () => {
    instrumentation.manuallyInstrument(namespace);
    instrumentation.manuallyInstrument(namespace);

    expect(isWrapped(configureSync())).toBe(true);
    expect(isWrapped(configureSync().__original)).toBe(false);
  });

  it("still exports one span per run", async () => {
    instrumentation.manuallyInstrument(namespace);
    instrumentation.manuallyInstrument(namespace);

    const chain = RunnableLambda.from((input: number) => input + 1).withConfig({ runName: "add_one" });
    expect(await chain.invoke(1)).toBe(2);
    await awaitAllCallbacks();
    await provider.forceFlush();

    expect(exporter.getFinishedSpans().map((span) => span.name)).toEqual(["add_one"]);
  });
});
