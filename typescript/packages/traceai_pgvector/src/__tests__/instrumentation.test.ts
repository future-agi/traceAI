import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { createServer } from "http";
import { AddressInfo } from "net";
import { PgVectorInstrumentation, isPatched } from "../instrumentation";

// Real pg query normalization, return contracts, and Pool -> Client delegation.
// Only transport is replaced: nothing connects to a database or vendor.
const versions = [
  ["8.0.0 (floor)", require("pg-floor")],
  ["8.23.1 (current)", require("pg")],
] as const;
const sql = "SELECT id FROM documents ORDER BY embedding <=> $1::vector LIMIT $2";
const result = { rows: [{ id: 1, embedding: "private-result-vector" }], rowCount: 1 };
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

function controlledClient(pg: any) {
  const client = new pg.Client({ host: "127.0.0.1", user: "synthetic", database: "synthetic" });
  const submitted: any[] = [];
  client._pulseQueryQueue = () => {
    const queue = client._queryQueue ?? client.queryQueue;
    if (queue.length) submitted.push(queue.shift());
  };
  return { client, submitted };
}

for (const [version, pg] of versions) {
  describe(`pg ${version}`, () => {
    let instrumentation: PgVectorInstrumentation;
    let exporter: InMemorySpanExporter;
    let provider: BasicTracerProvider;
    let manager: AsyncLocalStorageContextManager;
    let originalQuery: Function;
    let originalPoolQuery: Function;

    beforeEach(() => {
      originalQuery = pg.Client.prototype.query;
      originalPoolQuery = pg.Pool.prototype.query;
      exporter = new InMemorySpanExporter();
      provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      manager = new AsyncLocalStorageContextManager().enable();
      context.setGlobalContextManager(manager);
      instrumentation = new PgVectorInstrumentation({ instrumentationConfig: { enabled: false } });
      instrumentation.setTracerProvider(provider);
      instrumentation.enable();
      instrumentation.manuallyInstrument(pg);
    });

    afterEach(async () => {
      instrumentation.disable();
      // Also clean up the broken baseline so independent tests still diagnose it.
      pg.Client.prototype.query = originalQuery;
      pg.Pool.prototype.query = originalPoolQuery;
      context.disable();
      manager.disable();
      await provider.shutdown();
    });

    test.each(["argument", "config"])("callback %s retains return and ends on database error", async (form) => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      const returned = form === "argument"
        ? client.query(sql, ["[0.1,0.2]", 5], callback)
        : client.query({ text: sql, values: ["[0.1,0.2]", 5], callback });
      await tick();
      const earlySpans = exporter.getFinishedSpans().length;
      const error = new Error("synthetic database failure");
      submitted[0].callback(error);
      await tick();
      expect(returned).toBeUndefined();
      expect(earlySpans).toBe(0);
      expect(callback).toHaveBeenCalledWith(error);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    });

    test("Promise settles with the original result and vector metadata", async () => {
      const { client, submitted } = controlledClient(pg);
      const pending = client.query({ text: sql, values: ["[0.1,0.2,0.3]", 5] });
      expect(pending).toBeInstanceOf(Promise);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      submitted[0].callback(null, result);
      expect(await pending).toBe(result);
      const [span] = exporter.getFinishedSpans();
      expect(span.name).toBe("pgvector vectorSearch");
      expect(span.attributes).toMatchObject({
        "fi.span.kind": "VECTOR_DB",
        "db.vector.distance_metric": "cosine",
        "db.vector.query.dimensions": 3,
        "db.vector.query.top_k": 5,
        "db.response.returned_rows": 1,
        "db.response.affected_rows": 1,
      });
      expect(span.attributes["db.statement"]).toBeUndefined();
      expect(JSON.stringify(span.attributes)).not.toContain("private-result-vector");
    });

    test("Promise rejection preserves the database error", async () => {
      const { client, submitted } = controlledClient(pg);
      const pending = client.query(sql);
      const error = new Error("synthetic rejected query");
      submitted[0].callback(error);
      await expect(pending).rejects.toBe(error);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    });

    test("synchronous validation errors stay synchronous", () => {
      const { client } = controlledClient(pg);
      let caught: unknown;
      let returned: any;
      try { returned = client.query(null); } catch (error) { caught = error; }
      // Consume a rejection from the old async wrapper without changing the assertion.
      returned?.catch(() => {});
      expect(caught).toBeInstanceOf(TypeError);
    });

    test.each([false, true])("real Pool delegation produces one execution span (callback=%s)", async (withCallback) => {
      const { client, submitted } = controlledClient(pg);
      const pool = new pg.Pool();
      const release = jest.fn();
      client.release = release;
      pool.connect = (callback: Function) => process.nextTick(() => callback(null, client, release));
      const callback = jest.fn();
      const pending = withCallback ? pool.query(sql, callback) : pool.query(sql);
      await tick();
      submitted[0].callback(null, result);
      if (!withCallback) expect(await pending).toBe(result);
      await tick();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(release).toHaveBeenCalledTimes(1);
      if (withCallback) {
        expect(pending).toBeUndefined();
        expect(callback).toHaveBeenCalledWith(undefined, result);
      }
      await pool.end();
    });

    test("Query Submittable retains identity and waits for end", () => {
      const { client } = controlledClient(pg);
      const query = new pg.Query(sql);
      const originalEmit = query.emit;
      const onEnd = jest.fn();
      query.on("end", onEnd);
      const returned = client.query(query);
      expect(returned).toBe(query);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      query.emit("row", { id: 1 });
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      query.emit("end", result);
      expect(query.emit).toBe(originalEmit);
      expect(onEnd).toHaveBeenCalledWith(result);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    test("Query error events retain EventEmitter's unhandled-error contract", () => {
      const { client } = controlledClient(pg);
      const query = new pg.Query(sql);
      client.query(query);
      const error = new Error("synthetic unhandled query error");
      expect(() => new pg.Query(sql).emit("error", error)).toThrow(/synthetic unhandled query error/);
      expect(() => query.emit("error", error)).toThrow(/synthetic unhandled query error/);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    });

    test("Query callback keeps this, arguments, and thrown application errors", () => {
      const { client, submitted } = controlledClient(pg);
      const applicationError = new Error("application callback failed");
      const receiver = {};
      const callback = jest.fn(function (this: any, ...args: any[]) {
        expect(this).toBe(receiver);
        expect(args).toEqual([null, result]);
        throw applicationError;
      });
      const query = new pg.Query({ text: sql, callback });
      client.query(query);
      expect(() => submitted[0].callback.call(receiver, null, result)).toThrow(applicationError);
      expect(query.callback).toBe(callback);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.OK);
    });

    test("completion callbacks run under caller context, not the database span", () => {
      const { client, submitted } = controlledClient(pg);
      const parent = provider.getTracer("test").startSpan("caller");
      const seen: string[] = [];
      context.with(trace.setSpan(context.active(), parent), () => {
        client.query(sql, () => seen.push(trace.getActiveSpan()!.spanContext().spanId));
      });
      submitted[0].callback(null, result);
      expect(seen).toEqual([parent.spanContext().spanId]);
      parent.end();
    });

    test("ordinary SQL is not classified as VECTOR_DB", async () => {
      const { client, submitted } = controlledClient(pg);
      const pending = client.query("SELECT '<=>' AS operator /* <-> */ FROM documents");
      submitted[0].callback(null, result);
      await pending;
      expect(exporter.getFinishedSpans()[0].attributes["fi.span.kind"]).toBeUndefined();
    });

    test("disable restores originals and enable can instrument again", async () => {
      expect(isPatched()).toBe(true);
      instrumentation.disable();
      expect(pg.Client.prototype.query).toBe(originalQuery);
      expect(pg.Pool.prototype.query).toBe(originalPoolQuery);
      expect(isPatched()).toBe(false);
      instrumentation.enable();
      instrumentation.manuallyInstrument(pg);
      instrumentation.manuallyInstrument(pg);
      const { client, submitted } = controlledClient(pg);
      const pending = client.query(sql);
      submitted[0].callback(null, result);
      await pending;
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    test("config changes take effect without repatching", async () => {
      const config = { enabled: true, captureQueries: true };
      instrumentation.setConfig(config);
      const { client, submitted } = controlledClient(pg);
      const pending = client.query(sql);
      submitted[0].callback(null, result);
      await pending;
      expect(exporter.getFinishedSpans()[0].attributes["db.statement"]).toBe(sql);
    });

    test("frozen callback configs are not mutated", () => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      const config = Object.freeze({ text: sql, values: ["[1,2]", 5], callback });
      expect(client.query(config)).toBeUndefined();
      submitted[0].callback(null, result);
      expect(config.callback).toBe(callback);
      expect(callback).toHaveBeenCalledWith(null, result);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    test("callback configs retain inherited and non-enumerable query fields", () => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      const config = Object.create({ text: sql });
      Object.defineProperty(config, "values", { value: ["[1,2]", 5] });
      config.callback = callback;
      expect(client.query(config)).toBeUndefined();
      expect(submitted[0].text).toBe(sql);
      expect(submitted[0].values).toEqual(["[1,2]", 5]);
      submitted[0].callback(null, result);
      expect(config.callback).toBe(callback);
    });

    test("a second instrumentor cannot duplicate or remove the first one's patch", async () => {
      const second = new PgVectorInstrumentation({ instrumentationConfig: { enabled: false } });
      second.setTracerProvider(provider);
      second.enable();
      second.manuallyInstrument(pg);
      second.disable();
      const { client, submitted } = controlledClient(pg);
      const pending = client.query(sql);
      submitted[0].callback(null, result);
      await pending;
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    test("in-flight callbacks still complete after disable", () => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      client.query(sql, callback);
      instrumentation.disable();
      submitted[0].callback(null, result);
      expect(callback).toHaveBeenCalledWith(null, result);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    test("a callback passed alongside a Query preserves identity and precedence", () => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      const ignored = jest.fn();
      const query = new pg.Query({ text: sql, callback });
      expect(client.query(query, ignored)).toBe(query);
      submitted[0].callback(null, result);
      expect(callback).toHaveBeenCalledTimes(1);
      expect(ignored).not.toHaveBeenCalled();
      expect(query.callback).toBe(callback);
    });

    test("Query with callback argument completes without becoming a Promise", () => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      const query = new pg.Query(sql);
      expect(client.query(query, callback)).toBe(query);
      submitted[0].callback(null, result);
      expect(query.callback).toBe(callback);
      expect(callback).toHaveBeenCalledWith(null, result);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    test("immutable custom Submittables pass through without inventing completion", () => {
      const { client, submitted } = controlledClient(pg);
      const callback = jest.fn();
      const query = Object.freeze({ submit() {}, callback });
      expect(client.query(query)).toBe(query);
      submitted[0].callback(null, result);
      expect(callback).toHaveBeenCalledWith(null, result);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
    });

    test("captureStatements alias works and explicit captureQueries false wins", async () => {
      const { client, submitted } = controlledClient(pg);
      instrumentation.setConfig({ captureStatements: true });
      const first = client.query(sql);
      submitted[0].callback(null, result);
      await first;
      expect(exporter.getFinishedSpans()[0].attributes["db.statement"]).toBe(sql);
      instrumentation.setConfig({ captureStatements: true, captureQueries: false });
      const second = client.query(sql);
      submitted[1].callback(null, result);
      await second;
      expect(exporter.getFinishedSpans()[1].attributes["db.statement"]).toBeUndefined();
    });

    test("environment privacy settings suppress opt-in SQL and error text", async () => {
      const prior = process.env.FI_HIDE_INPUTS;
      process.env.FI_HIDE_INPUTS = "true";
      try {
        instrumentation.setConfig({ captureQueries: true });
        const { client, submitted } = controlledClient(pg);
        const pending = client.query(sql + " /* private-sql */");
        submitted[0].callback(new Error("private-error"));
        await expect(pending).rejects.toThrow("private-error");
        const [span] = exporter.getFinishedSpans();
        expect(span.attributes["db.statement"]).toBeUndefined();
        expect(span.status.message).toBeUndefined();
        expect(JSON.stringify(span.events)).not.toContain("private-error");
      } finally {
        if (prior === undefined) delete process.env.FI_HIDE_INPUTS;
        else process.env.FI_HIDE_INPUTS = prior;
      }
    });

    test("exports OTLP with one error span and no hidden SQL, vectors, or error text", async () => {
      const bodies: any[] = [];
      const server = createServer((request, response) => {
        const chunks: Buffer[] = [];
        request.on("data", (chunk) => chunks.push(chunk));
        request.on("end", () => {
          bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
          response.writeHead(200, { "content-type": "application/json" });
          response.end("{}");
        });
      });
      await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
      const port = (server.address() as AddressInfo).port;
      const wireExporter = new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` });
      const wireProvider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(wireExporter)] });
      instrumentation.disable();
      instrumentation = new PgVectorInstrumentation({
        instrumentationConfig: { enabled: false, captureQueries: true, captureQueryVectors: true, captureResultVectors: true },
        traceConfig: { hideInputs: true, hideOutputs: true },
      });
      instrumentation.setTracerProvider(wireProvider);
      instrumentation.enable();
      instrumentation.manuallyInstrument(pg);
      try {
        const { client, submitted } = controlledClient(pg);
        const pending = client.query(sql + " /* private-sql-sentinel */", ["[0.12345,0.67890]", 5]);
        submitted[0].callback(new Error("private-error-sentinel"));
        await expect(pending).rejects.toThrow("private-error-sentinel");
        await wireProvider.forceFlush();
        const spans = bodies.flatMap((body) => body.resourceSpans.flatMap((resource: any) =>
          resource.scopeSpans.flatMap((scope: any) => scope.spans)));
        expect(spans).toHaveLength(1);
        expect(spans[0].status.code).toBe(2);
        expect(JSON.stringify(bodies)).not.toMatch(/private-sql-sentinel|private-error-sentinel|0\.12345|db.statement/);
        expect(spans[0].attributes).toEqual(expect.arrayContaining([
          { key: "db.vector.distance_metric", value: { stringValue: "cosine" } },
        ]));
      } finally {
        instrumentation.disable();
        await wireProvider.shutdown();
        await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
      }
    });
  });
}
