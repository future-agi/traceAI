import path from "node:path";
import http from "node:http";
import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { WeaviateInstrumentation, isPatched } from "../instrumentation";

// Both endpoints use the actual installed SDK. Only the transport boundary is fake.
const sdkName = process.env.WEAVIATE_TEST_PACKAGE || "weaviate-client";
const sdk = require(sdkName);
const { ConnectionGRPC } = require(
  path.join(path.dirname(require.resolve(sdkName)), "connection/index.js"),
);
const ID = "00000000-0000-0000-0000-000000000001";
const manager = new AsyncLocalStorageContextManager();

let exporter: InMemorySpanExporter;
let provider: BasicTracerProvider;
let instrumentation: WeaviateInstrumentation;
let connection: any;
let transport: jest.Mock;
let connectSpy: jest.SpyInstance;
let grpcAggregates: boolean;

function configure(config: any = {}, traceConfig: any = {}) {
  instrumentation?.disable();
  instrumentation = new WeaviateInstrumentation({
    instrumentationConfig: config,
    traceConfig,
  });
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
}

async function client() {
  return sdk.default.client({
    connectionParams: {
      http: { host: "localhost", port: 8080, secure: false },
      grpc: { host: "localhost", port: 50051, secure: false },
    },
    skipInitChecks: true,
  });
}

beforeAll(() => context.setGlobalContextManager(manager.enable()));
afterAll(() => {
  context.disable();
  manager.disable();
});
beforeEach(() => {
  exporter = new InMemorySpanExporter();
  provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  transport = jest.fn(async () => ({ results: [] }));
  const search = Object.fromEntries(
    [
      "withFetch",
      "withNearText",
      "withNearVector",
      "withHybrid",
      "withBm25",
      "withNearObject",
    ].map((name) => [name, transport]),
  );
  connection = {
    http: {},
    getDetails: () => ({}),
    close: jest.fn(),
    postReturn: jest.fn(async (url: string, body: any) =>
      url === "/schema"
        ? typeof body === "string"
          ? JSON.parse(body)
          : body
        : { id: ID },
    ),
    postEmpty: jest.fn(async () => undefined),
    put: jest.fn(async () => undefined),
    patch: jest.fn(async () => undefined),
    delete: jest.fn(async () => undefined),
    query: jest.fn(async () => ({
      data: {
        Aggregate: {
          Articles: [
            {
              meta: { count: 3 },
              groupedBy: { path: ["title"], value: "offline" },
            },
          ],
        },
      },
    })),
    search: jest.fn(async () => search),
    batch: jest.fn(async () => ({
      withObjects: async () => ({ errors: [] }),
      withDelete: async () => ({
        matches: 2,
        successful: 2,
        failed: 0,
        objects: [],
      }),
    })),
  };
  grpcAggregates = false;
  connection.aggregate = jest.fn(async () =>
    Object.fromEntries(
      [
        "withFetch",
        "withNearText",
        "withNearVector",
        "withHybrid",
        "withNearObject",
      ].map((name) => [
        name,
        async () => ({
          singleResult: { objectsCount: 3 },
          groupedResults: {
            groups: [
              {
                objectsCount: 3,
                groupedBy: { path: ["title"], text: "offline" },
              },
            ],
          },
        }),
      ]),
    ),
  );
  const support = new Proxy(
    { getVersion: async () => "1.35.0" },
    {
      get(target: any, key: string) {
        return (
          target[key] ||
          (async () => ({
            supports: key === "supportsAggregateGRPC" ? grpcAggregates : true,
          }))
        );
      },
    },
  );
  connectSpy = jest.spyOn(ConnectionGRPC, "use").mockResolvedValue({
    connection,
    dbVersionSupport: support,
    dbVersionProvider: {},
  });
  configure();
});
afterEach(async () => {
  instrumentation.disable();
  connectSpy.mockRestore();
  await provider.shutdown();
});

it("instruments actual factories, not nonexistent exported classes", async () => {
  expect(sdk.WeaviateClient).toBeUndefined();
  expect(sdk.Collection).toBeUndefined();
  expect(isPatched()).toBe(true);
  const c = await client();
  const collection = c.collections.get("Articles");
  expect(collection).not.toBeInstanceOf(Promise);
  expect(await collection.data.insert({ title: "private document" })).toBe(ID);
  expect(
    await collection
      .withTenant("tenant-a")
      .withConsistency("QUORUM")
      .query.fetchObjects({ limit: 2 }),
  ).toEqual({ objects: [] });
  expect(connection.search).toHaveBeenCalledWith(
    "Articles",
    "QUORUM",
    "tenant-a",
    ...(connection.search.mock.calls[0].length === 4 ? [undefined] : []),
  );
  expect(exporter.getFinishedSpans().map((s) => s.name)).toEqual([
    "weaviate insert",
    "weaviate fetchObjects",
  ]);
  for (const span of exporter.getFinishedSpans()) {
    expect(span.kind).toBe(2);
    expect(span.attributes).toMatchObject({
      "db.system": "weaviate",
      "db.collection.name": "Articles",
      "fi.span.kind": "VECTOR_DB",
    });
  }
});

it.each([
  "connectToLocal",
  "connectToCustom",
  "connectToWeaviateCloud",
  "connectToWCD",
  "connectToWCS",
])("wraps default %s without duplicate spans", async (name) => {
  const warning = jest.spyOn(console, "warn").mockImplementation(() => {});
  try {
    const opts = {
      httpHost: "localhost",
      httpPort: 8080,
      httpSecure: false,
      grpcHost: "localhost",
      grpcPort: 50051,
      grpcSecure: false,
      skipInitChecks: true,
    };
    const c =
      name.includes("Cloud") ||
      name === "connectToWCD" ||
      name === "connectToWCS"
        ? await sdk.default[name]("https://offline.weaviate.network", opts)
        : await sdk.default[name](opts);
    await c.collections.get("Articles").query.fetchObjects();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  } finally {
    warning.mockRestore();
  }
});

it("wraps named connection exports when available", async () => {
  if (!sdk.connectToLocal) return; // The v3.0.0 API only has default factories.
  await (await sdk.connectToLocal({ skipInitChecks: true })).collections
    .get("Articles")
    .query.fetchObjects();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("wraps get/use and asynchronous collection creators", async () => {
  const c = await client();
  for (const method of [
    "get",
    "use",
    "create",
    "createFromSchema",
    "createFromJson",
  ]) {
    if (!c.collections[method]) continue;
    const arg =
      method === "create"
        ? { name: "Articles" }
        : method === "createFromSchema"
          ? { class: "Articles" }
          : method === "createFromJson"
            ? JSON.stringify({ class: "Articles" })
            : "Articles";
    const collection = await c.collections[method](arg);
    await collection
      .withConsistency("ONE")
      .withTenant({ name: "tenant-a" })
      .query.fetchObjects();
  }
  const count = [
    "get",
    "use",
    "create",
    "createFromSchema",
    "createFromJson",
  ].filter((name) => c.collections[name]).length;
  expect(exporter.getFinishedSpans()).toHaveLength(count);
});

it("records positional query arguments and real response counts", async () => {
  const q = (await client()).collections.get("Articles").query;
  await q.nearVector([0.1, 0.2], { limit: 4, distance: 0.2 });
  await q.nearText("private query", { limit: 2 });
  await q.hybrid("private query", { limit: 3, alpha: 0 });
  await q.bm25("private query", { limit: 1 });
  await q.fetchObjectById(ID);
  await q.nearObject(ID, { limit: 1 });
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(6);
  expect(spans[0].attributes).toMatchObject({
    "db.vector.query.dimensions": 2,
    "db.vector.query.top_k": 4,
    "db.response.returned_rows": 0,
  });
  expect(spans[2].attributes["db.vector.query.alpha"]).toBe(0);
  expect(JSON.stringify(spans.map((s) => s.attributes))).not.toContain(
    "private query",
  );
});

it("traces writes and object-indexed batch results", async () => {
  const collection = (await client()).collections.get("Articles");
  await collection.data.insertMany([
    { properties: { title: "one" } },
    { properties: { title: "two" } },
  ]);
  await collection.data.update({ id: ID, properties: { title: "updated" } });
  await collection.data.replace({ id: ID, properties: { title: "replaced" } });
  await collection.data.deleteById(ID);
  await collection.data.deleteMany(
    collection.filter.byProperty("title").equal("private"),
  );
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(5);
  expect(spans[0].attributes).toMatchObject({
    "db.operation.batch_size": 2,
    "db.response.inserted_count": 2,
  });
  expect(spans[4].attributes["db.operation.has_filter"]).toBe(true);
});

it("marks partial batch failures without exporting their payloads", async () => {
  connection.batch.mockResolvedValue({
    withObjects: async () => ({
      errors: [{ index: 0, error: "secret failure" }],
    }),
  });
  const result = await (await client()).collections
    .get("Articles")
    .data.insertMany([{ title: "private" }]);
  expect(result.hasErrors).toBe(true);
  const span = exporter.getFinishedSpans()[0];
  expect(span.status.code).toBe(SpanStatusCode.ERROR);
  expect(span.attributes["db.response.inserted_count"]).toBe(0);
  expect(JSON.stringify(span.attributes)).not.toContain("private");
});

it.each([false, true])(
  "traces aggregate and grouped aggregate methods (gRPC advertised: %s)",
  async (grpc) => {
    grpcAggregates = grpc;
    const a = (await client()).collections.get("Articles").aggregate;
    for (const target of [a, a.groupBy]) {
      await target.overAll({ groupBy: "title" });
      await target.nearText("private", { groupBy: "title", objectLimit: 2 });
      await target.nearVector([0.1, 0.2], { groupBy: "title", objectLimit: 2 });
      await target.nearObject(ID, { groupBy: "title", objectLimit: 2 });
      if (target.hybrid)
        await target.hybrid("private", { groupBy: "title", objectLimit: 2 });
    }
    const perGroup = a.hybrid ? 5 : 4; // Hybrid aggregation was added after 3.0.0.
    expect(exporter.getFinishedSpans()).toHaveLength(perGroup * 2);
    expect(
      exporter.getFinishedSpans()[0].attributes["db.response.total_count"],
    ).toBe(3);
    expect(exporter.getFinishedSpans()[perGroup].name).toBe(
      "weaviate aggregate.groupBy.overAll",
    );
  },
);

it("preserves active parents and records the original rejection exactly once", async () => {
  const parent = provider.getTracer("test").startSpan("parent");
  const error = new Error("transport failed");
  transport.mockImplementation(async () => {
    expect(trace.getActiveSpan()?.spanContext().spanId).not.toBe(
      parent.spanContext().spanId,
    );
    throw error;
  });
  const q = (await client()).collections.get("Articles").query;
  await context.with(trace.setSpan(context.active(), parent), async () => {
    await expect(q.fetchObjects()).rejects.toBe(error);
    expect(trace.getActiveSpan()?.spanContext().spanId).toBe(
      parent.spanContext().spanId,
    );
  });
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
  expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  expect(spans[0].events).toHaveLength(1);
  parent.end();
});

it("restores factories and existing objects; saved wrappers stay inert after re-enable", async () => {
  instrumentation.disable();
  const factory = sdk.default.client;
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk.default);
  const c = await client();
  const coll = c.collections.get("Articles").withTenant("tenant-a");
  const saved = coll.query.fetchObjects;
  instrumentation.manuallyInstrument(sdk);
  expect(coll.query.fetchObjects).toBe(saved);
  await coll.query.fetchObjects();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  instrumentation.disable();
  expect(isPatched()).toBe(false);
  expect(sdk.default.client).toBe(factory);
  expect(coll.query.fetchObjects).not.toBe(saved);
  await coll.query.fetchObjects();
  await saved.call(coll.query);
  await c.collections.get("Articles").query.fetchObjects();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
  await saved.call(coll.query);
  await (await client()).collections.get("Articles").query.fetchObjects();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

it("does not claim success for unsupported exports or patch while disabled", () => {
  instrumentation.disable();
  instrumentation.manuallyInstrument(sdk);
  expect(isPatched()).toBe(false);
  instrumentation = new WeaviateInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  instrumentation.manuallyInstrument(sdk);
  expect(isPatched()).toBe(false);
  instrumentation.enable();
  // A real module may be auto-patched by enable outside Jest; clear it first here.
  instrumentation.manuallyInstrument({});
  expect(isPatched()).toBe(false);
});

it("does not duplicate spans across instrumentation instances", async () => {
  const second = new WeaviateInstrumentation();
  second.setTracerProvider(provider);
  try {
    second.manuallyInstrument(sdk);
    await (await client()).collections.get("Articles").query.fetchObjects();
    second.disable();
    expect(isPatched()).toBe(true);
    await (await client()).collections.get("Articles").query.fetchObjects();
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  } finally {
    second.disable();
  }
});

it("hands existing and new collections to a co-owner when the first disables", async () => {
  const otherExporter = new InMemorySpanExporter();
  const otherProvider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(otherExporter)],
  });
  const second = new WeaviateInstrumentation();
  second.setTracerProvider(otherProvider);
  const factory = sdk.default.client;
  const c = await client();
  const existing = c.collections.get("Articles").withTenant("tenant-a");
  const saved = existing.query.fetchObjects;
  second.manuallyInstrument(sdk);
  try {
    await existing.query.fetchObjects();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(otherExporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.disable();
    expect(isPatched()).toBe(true);
    expect(sdk.default.client).toBe(factory);
    expect(existing.query.fetchObjects).toBe(saved);
    await saved.call(existing.query);
    await c.collections.get("Articles").query.fetchObjects();
    await (await client()).collections.get("Articles").query.fetchObjects();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(otherExporter.getFinishedSpans()).toHaveLength(3);
    second.disable();
    expect(isPatched()).toBe(false);
    expect(sdk.default.client).not.toBe(factory);
    expect(existing.query.fetchObjects).not.toBe(saved);
    await existing.query.fetchObjects();
    expect(otherExporter.getFinishedSpans()).toHaveLength(3);
  } finally {
    second.disable();
    await otherProvider.shutdown();
  }
});

it("restores only the disabled owner's unrelated module patches", async () => {
  const separate = { client: sdk.default.client };
  instrumentation.manuallyInstrument(separate);
  const second = new WeaviateInstrumentation();
  second.setTracerProvider(provider);
  second.manuallyInstrument(sdk);
  const extraFactory = separate.client;
  try {
    instrumentation.disable();
    expect(separate.client).not.toBe(extraFactory);
    expect(isPatched()).toBe(true);
    await (await client()).collections.get("Articles").query.fetchObjects();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  } finally {
    second.disable();
  }
});

it("transfers an in-flight client to the remaining co-owner", async () => {
  const second = new WeaviateInstrumentation();
  second.setTracerProvider(provider);
  second.manuallyInstrument(sdk);
  let finish!: (value: any) => void;
  const original = connectSpy.getMockImplementation()!;
  connectSpy.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  try {
    const pending = client();
    instrumentation.disable();
    finish(await original());
    const c = await pending;
    await c.collections.get("Articles").query.fetchObjects();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  } finally {
    second.disable();
  }
});

it("does not patch clients whose connection completes after disable", async () => {
  let finish!: (value: any) => void;
  const original = connectSpy.getMockImplementation()!;
  connectSpy.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const pending = client();
  instrumentation.disable();
  finish(await original());
  const c = await pending;
  await c.collections.get("Articles").query.fetchObjects();
  expect(exporter.getFinishedSpans()).toHaveLength(0);
});

function replyWithContent() {
  return {
    results: [
      {
        metadata: {
          id: ID,
          vectorBytes: Buffer.from(new Float32Array([123.5, 789.5]).buffer),
          vector: [],
          vectors: [],
        },
        properties: {
          nonRefProps: {
            fields: { title: { textValue: "WIRE_SECRET_RESULT" } },
          },
          refProps: [],
        },
      },
    ],
  };
}

it("captures only opted-in content and isolates document/vector flags", async () => {
  transport.mockResolvedValue(replyWithContent());
  configure({ captureDocuments: true });
  let coll = (await client()).collections.get("Articles");
  await coll.query.nearText("captured query");
  await coll.data.insert({ vectors: [999, 888] });
  let spans = exporter.getFinishedSpans();
  expect(spans[0].attributes["input.value"]).toContain("captured query");
  expect(spans[0].attributes["output.value"]).toContain("WIRE_SECRET_RESULT");
  expect(spans[0].attributes["output.value"]).not.toContain("123.5");
  expect(JSON.stringify(spans[1].attributes)).not.toContain("999");
  configure({ captureQueryVectors: true, captureResultVectors: true });
  coll = (await client()).collections.get("Articles");
  await coll.query.nearVector([123.5, 789.5]);
  spans = exporter.getFinishedSpans();
  expect(spans[2].attributes["input.value"]).toContain("123.5");
  expect(spans[2].attributes["output.value"]).toContain("789.5");
  expect(spans[2].attributes["output.value"]).not.toContain(
    "WIRE_SECRET_RESULT",
  );
});

it("patches multiple module objects independently", async () => {
  instrumentation.disable();
  const separate = { client: sdk.default.client };
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
  instrumentation.manuallyInstrument(separate);
  const c = await separate.client({
    connectionParams: {
      http: { host: "localhost", port: 8080 },
      grpc: { host: "localhost", port: 50051 },
    },
  });
  await c.collections.get("Articles").query.fetchObjects();
  await (await client()).collections.get("Articles").query.fetchObjects();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

it("preserves a synchronous SDK validation error", async () => {
  const coll = (await client()).collections.get("Articles");
  let error: unknown;
  try {
    coll.query.fetchObjects.call(null);
  } catch (e) {
    error = e;
  }
  expect(error).toBeInstanceOf(Error);
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("exports metadata-only OTLP by default and applies content/error privacy on the wire", async () => {
  const bodies: any[] = [];
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as any).port;
  const wire = new BasicTracerProvider({
    spanProcessors: [
      new SimpleSpanProcessor(
        new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` }),
      ),
    ],
  });
  try {
    instrumentation.setTracerProvider(wire);
    transport.mockResolvedValue(replyWithContent());
    let coll = (await client()).collections.get("Articles");
    await coll.query.nearText("WIRE_SECRET_QUERY", { limit: 1 });
    await coll.data.insert({ title: "WIRE_SECRET_DOCUMENT" });
    configure(
      {
        captureDocuments: true,
        captureQueryVectors: true,
        captureResultVectors: true,
      },
      { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
    );
    instrumentation.setTracerProvider(wire);
    coll = (await client()).collections.get("Articles");
    await coll.query.nearText("WIRE_SECRET_CAPTURED");
    await coll.query.nearVector([123.456, 789.012]);
    const err = new Error("WIRE_SECRET_ERROR");
    transport.mockRejectedValue(err);
    await expect(coll.query.fetchObjects()).rejects.toBe(err);
    await wire.forceFlush();
    const spans = bodies.flatMap((b) =>
      b.resourceSpans.flatMap((r: any) =>
        r.scopeSpans.flatMap((s: any) => s.spans),
      ),
    );
    expect(spans).toHaveLength(5);
    const wireText = JSON.stringify(bodies);
    expect(wireText).not.toContain("WIRE_SECRET");
    expect(wireText).not.toContain("123.456");
    expect(wireText).toContain("weaviate nearVector");
    expect(wireText).toContain("db.collection.name");
    expect(wireText).toContain("__REDACTED__");
    expect(spans[4].status.code).toBe(2);
  } finally {
    await wire.shutdown();
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }
});
