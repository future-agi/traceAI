import { createServer as createTCPServer, Socket } from "node:net";
import { createServer as createHTTPServer } from "node:http";
import { AddressInfo } from "node:net";
import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { MongoDBInstrumentation, isPatched } from "./instrumentation";
import type { MongoClient } from "mongodb";

// A loopback MongoDB wire peer, not a MongoDB service or a cursor mock. Both
// drivers execute their real connect/findOne/cursor/iteration/error paths.
async function wirePeer(mongo: typeof import("mongodb")) {
  const { BSON } = mongo;
  const sockets = new Set<Socket>();
  const commands: Record<string, any>[] = [];
  let release: (() => void) | undefined;
  const server = createTCPServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    let buffered = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      buffered = Buffer.concat([buffered, chunk]);
      while (buffered.length >= 4 && buffered.length >= buffered.readInt32LE(0)) {
        const size = buffered.readInt32LE(0);
        const message = buffered.subarray(0, size);
        buffered = buffered.subarray(size);
        const requestId = message.readInt32LE(4);
        const op = message.readInt32LE(12);
        let command: Record<string, any> = {};
        if (op === 2004) {
          const start = message.indexOf(0, 20) + 1 + 8;
          command = BSON.deserialize(message.subarray(start));
        } else {
          let offset = 20;
          while (offset < message.length) {
            const kind = message[offset++];
            const sectionSize = message.readInt32LE(offset);
            if (kind === 0) {
              command = BSON.deserialize(message.subarray(offset, offset + sectionSize));
              offset += sectionSize;
            } else {
              const end = offset + sectionSize;
              const nameEnd = message.indexOf(0, offset + 4);
              const name = message.toString("utf8", offset + 4, nameEnd);
              offset = nameEnd + 1;
              command[name] = [];
              while (offset < end) {
                const documentSize = message.readInt32LE(offset);
                command[name].push(BSON.deserialize(message.subarray(offset, offset + documentSize)));
                offset += documentSize;
              }
            }
          }
        }
        commands.push(command);
        const reply = (document: Record<string, any>) => {
          const body = BSON.serialize(document);
          const headerLength = op === 2004 ? 36 : 21;
          const response = Buffer.alloc(headerLength + body.length);
          response.writeInt32LE(response.length, 0);
          response.writeInt32LE(1, 4);
          response.writeInt32LE(requestId, 8);
          response.writeInt32LE(op === 2004 ? 1 : 2013, 12);
          if (op === 2004) response.writeInt32LE(1, 32);
          Buffer.from(body).copy(response, headerLength);
          socket.write(response);
        };
        if (command.ismaster || command.isMaster || command.hello) {
          reply({ ok: 1, ismaster: true, isWritablePrimary: true, helloOk: true,
            minWireVersion: 0, maxWireVersion: 21, logicalSessionTimeoutMinutes: 30,
            maxBsonObjectSize: 16777216, maxMessageSizeBytes: 48000000, maxWriteBatchSize: 100000 });
        } else if (command.find || command.aggregate) {
          const send = () => {
            if (command.filter?.fail) {
              reply({ ok: 0, code: 2, codeName: "BadValue", errmsg: "secret-query-in-error" });
            } else {
              reply({ ok: 1, cursor: { id: command.filter?.multi ? BSON.Long.fromNumber(42) : BSON.Long.ZERO,
                ns: `synthetic.${command.find ?? command.aggregate}`,
                firstBatch: [{ _id: 1, private: "secret-result", embedding: [0.123456789] }] } });
            }
          };
          if (command.filter?.hold) release = send;
          else send();
        } else if (command.getMore) {
          reply({ ok: 1, cursor: { id: BSON.Long.ZERO, ns: "synthetic.rows", nextBatch: [{ _id: 2 }] } });
        } else if (command.insert) {
          reply(command.documents?.[0]?.writeError
            ? { ok: 1, n: 0, writeErrors: [{ index: 0, code: 11000, errmsg: "secret-write-error" }] }
            : { ok: 1, n: command.documents.length });
        } else if (command.update) {
          reply({ ok: 1, n: 1, nModified: 1 });
        } else if (command.delete) {
          reply({ ok: 1, n: 1 });
        } else reply({ ok: 1 });
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    uri: `mongodb://127.0.0.1:${(server.address() as AddressInfo).port}/?directConnection=true`,
    commands,
    release: () => { if (!release) throw new Error("No pending command"); release(); },
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}

const drivers = [
  ["5.0.0", require("mongodb-floor")],
  ["6.21.0", require("mongodb")],
  ["7.8.0", require("mongodb-current")],
] as const;

describe.each(drivers)("mongodb %s command monitoring", (_version, mongo) => {
  let peer: Awaited<ReturnType<typeof wirePeer>>;
  let instrumentation: MongoDBInstrumentation;
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let client: MongoClient;
  let extraClients: MongoClient[];
  let originalConnect: typeof mongo.MongoClient.prototype.connect;
  let originalFind: typeof mongo.Collection.prototype.find;
  let originalAggregate: typeof mongo.Collection.prototype.aggregate;
  let manager: AsyncLocalStorageContextManager;

  beforeEach(async () => {
    peer = await wirePeer(mongo);
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    manager = new AsyncLocalStorageContextManager().enable();
    context.setGlobalContextManager(manager);
    originalConnect = mongo.MongoClient.prototype.connect;
    originalFind = mongo.Collection.prototype.find;
    originalAggregate = mongo.Collection.prototype.aggregate;
    instrumentation = new MongoDBInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(mongo);
    client = new mongo.MongoClient(peer.uri, { serverSelectionTimeoutMS: 1000 });
    extraClients = [];
    await client.connect();
  });

  afterEach(async () => {
    for (const extra of extraClients) await extra.close();
    await client.close();
    instrumentation.disable();
    await provider.shutdown();
    context.disable();
    manager.disable();
    await peer.close();
  });

  test("leaves find/aggregate synchronous and lazy, including native validation", async () => {
    const collection = client.db("synthetic").collection("rows");
    const find = collection.find({ private: "secret-query" }).limit(1).sort({ _id: 1 });
    const aggregate = collection.aggregate([{ $match: {} }]);
    expect(find).toBeInstanceOf(mongo.FindCursor);
    expect(aggregate).toBeInstanceOf(mongo.AggregationCursor);
    expect(mongo.Collection.prototype.find).toBe(originalFind);
    expect(mongo.Collection.prototype.aggregate).toBe(originalAggregate);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(peer.commands.filter((command) => command.find || command.aggregate)).toHaveLength(0);
    expect(() => collection.aggregate({} as never)).toThrow();
    await find.close();
    await aggregate.close();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  test("findOne and aggregate.toArray retain driver return values", async () => {
    const collection = client.db("synthetic").collection("rows");
    const result = await collection.findOne({ private: "secret-query" });
    expect(result?._id).toBe(1);
    const results = await collection.aggregate([{ $match: {} }]).toArray();
    expect(results).toHaveLength(1);
    expect(exporter.getFinishedSpans().map((span) => span.name)).toEqual(["mongodb find", "mongodb aggregate"]);
  });

  test("lazy async iteration traces only executed find/getMore commands", async () => {
    const result = [];
    for await (const doc of client.db("synthetic").collection("rows").find({ multi: true })) result.push(doc);
    expect(result.map((doc) => doc._id)).toEqual([1, 2]);
    expect(exporter.getFinishedSpans().map((span) => span.name)).toEqual(["mongodb find", "mongodb getMore"]);
    expect(exporter.getFinishedSpans()[1].attributes["db.response.returned_rows"]).toBe(1);
  });

  test("early close does not drain a cursor", async () => {
    const cursor = client.db("synthetic").collection("rows").find({ multi: true });
    expect((await cursor.next())?._id).toBe(1);
    await cursor.close();
    expect(peer.commands.filter((command) => command.getMore)).toHaveLength(0);
    expect(exporter.getFinishedSpans().map((span) => span.name)).toEqual(["mongodb find", "mongodb killCursors"]);
  });

  test("keeps parent context across concurrent real-driver commands", async () => {
    const tracer = provider.getTracer("parent");
    const parents = [tracer.startSpan("first"), tracer.startSpan("second")];
    await Promise.all(parents.map((parent) => context.with(trace.setSpan(context.active(), parent), async () => {
      await client.db("synthetic").collection("rows").findOne({});
    })));
    const spans = exporter.getFinishedSpans();
    expect(spans.map((span) => span.parentSpanContext?.spanId).sort()).toEqual(parents.map((parent) => parent.spanContext().spanId).sort());
    parents.forEach((span) => span.end());
  });

  test("preserves vector metadata without recording vectors, queries or documents", async () => {
    await client.db("synthetic").collection("rows").aggregate([{ $vectorSearch: {
      index: "vector_index", path: "embedding", queryVector: [0.123456789, 0.987654321],
      limit: 5, numCandidates: 20, filter: { private: "secret-query" },
    } }]).toArray();
    expect(exporter.getFinishedSpans()[0].attributes).toMatchObject({
      "db.system": "mongodb", "db.namespace": "synthetic", "db.collection.name": "rows",
      "db.operation.type": "vector_search", "fi.span.kind": "VECTOR_DB",
      "db.vector.index_name": "vector_index", "db.vector.query.top_k": 5,
      "db.vector.query.num_candidates": 20, "db.vector.query.dimensions": 2,
    });
    const exported = JSON.stringify(exporter.getFinishedSpans().map((span) => span.attributes));
    expect(exported).not.toMatch(/secret-|0\.123456789|0\.987654321/);
  });

  test("retains legacy knnBeta metadata", async () => {
    await client.db("synthetic").collection("rows").aggregate([{ $search: {
      index: "legacy", knnBeta: { vector: [1, 2], path: "embedding", k: 3 },
    } }]).toArray();
    expect(exporter.getFinishedSpans()[0].attributes).toMatchObject({
      "db.operation.type": "vector_search", "db.vector.query.top_k": 3,
      "db.vector.index_name": "legacy", "db.vector.query.dimensions": 2,
    });
  });

  test("returns write results unchanged and captures only counts", async () => {
    const collection = client.db("synthetic").collection("rows");
    expect((await collection.insertMany([{ _id: 1 }, { _id: 2 }] as never)).insertedCount).toBe(2);
    expect((await collection.updateOne({}, { $set: { private: "secret-query" } })).modifiedCount).toBe(1);
    expect((await collection.deleteOne({})).deletedCount).toBe(1);
    expect(exporter.getFinishedSpans().map((span) => span.status.code)).toEqual([1, 1, 1]);
    expect(exporter.getFinishedSpans()[0].attributes["db.response.inserted_count"]).toBe(2);
  });

  test("preserves command failures and hides error details by default", async () => {
    await expect(client.db("synthetic").collection("rows").findOne({ fail: true })).rejects.toThrow("secret-query-in-error");
    const span = exporter.getFinishedSpans()[0];
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(JSON.stringify({ status: span.status, events: span.events, attributes: span.attributes })).not.toContain("secret-query-in-error");
  });

  test("recognizes writeErrors inside commandSucceeded", async () => {
    await expect(client.db("synthetic").collection("rows").insertOne({ writeError: true })).rejects.toThrow("secret-write-error");
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(exporter.getFinishedSpans()[0].status.message).toBeUndefined();
  });

  test("disable restores connect, detaches listeners, and re-enable does not duplicate", async () => {
    instrumentation.manuallyInstrument(mongo);
    expect(client.listenerCount("commandStarted")).toBe(1);
    expect(isPatched()).toBe(true);
    instrumentation.disable();
    expect(mongo.MongoClient.prototype.connect).toBe(originalConnect);
    expect(client.listenerCount("commandStarted")).toBe(0);
    expect((client as MongoClient & { monitorCommands: boolean }).monitorCommands).toBe(false);
    await client.db("synthetic").collection("rows").findOne({});
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    await client.db("synthetic").collection("rows").findOne({});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(client.listenerCount("commandStarted")).toBe(1);
  });

  test("a second instrumentor cannot double-instrument the same module/client", async () => {
    const other = new MongoDBInstrumentation();
    try {
      other.setTracerProvider(provider);
      other.manuallyInstrument(mongo);
      other.instrumentClient(client);
      other.disable();
      await client.db("synthetic").collection("rows").findOne({});
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(client.listenerCount("commandStarted")).toBe(1);
    } finally { other.disable(); }
  });

  test("honors disabled construction and OTel suppression", async () => {
    const disabled = new MongoDBInstrumentation({ instrumentationConfig: { enabled: false } });
    disabled.manuallyInstrument(mongo);
    expect(disabled.isEnabled()).toBe(false);
    await context.with(suppressTracing(context.active()), () => client.db("synthetic").collection("rows").findOne({}));
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    disabled.disable();
  });

  test("attaches to a connected opt-in client and cleans up on close", async () => {
    instrumentation.disable();
    const extra = new mongo.MongoClient(peer.uri, { monitorCommands: true });
    extraClients.push(extra);
    await extra.connect();
    instrumentation.enable();
    instrumentation.instrumentClient(extra);
    await extra.db("synthetic").collection("rows").findOne({});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    await extra.close();
    expect(extra.listenerCount("commandStarted")).toBe(0);
    expect(extra.monitorCommands).toBe(true);
  });

  test("correlates requests independently across clients", async () => {
    const extra = new mongo.MongoClient(peer.uri);
    extraClients.push(extra);
    await extra.connect();
    await Promise.all([client, extra].map((current) => current.db("synthetic").collection("rows").findOne({})));
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  test("ends in-flight spans once when disabled, without reporting false success", async () => {
    const pending = client.db("synthetic").collection("rows").findOne({ hold: true });
    while (!peer.commands.some((command) => command.filter?.hold)) await new Promise((resolve) => setImmediate(resolve));
    instrumentation.disable();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.UNSET);
    peer.release();
    expect((await pending)?._id).toBe(1);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  test("OTLP HTTP wire contains command/vector metadata, not sensitive contents", async () => {
    const bodies: string[] = [];
    const receiver = createHTTPServer((request, response) => {
      let body = "";
      request.on("data", (chunk) => { body += chunk.toString(); });
      request.on("end", () => { bodies.push(body); response.writeHead(200, { "Content-Type": "application/json" }); response.end("{}"); });
    });
    await new Promise<void>((resolve) => receiver.listen(0, "127.0.0.1", resolve));
    const wireProvider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `http://127.0.0.1:${(receiver.address() as AddressInfo).port}/v1/traces`,
    }))] });
    try {
      instrumentation.setTracerProvider(wireProvider);
      await client.db("synthetic").collection("rows").aggregate([{ $vectorSearch: {
        index: "vector_index", queryVector: [0.123456789], limit: 2, filter: { private: "secret-query" },
      } }]).toArray();
      await expect(client.db("synthetic").collection("rows").findOne({ fail: true })).rejects.toThrow();
      await wireProvider.forceFlush();
      const wire = bodies.join("\n");
      expect(bodies).toHaveLength(2);
      expect(wire).toContain("db.vector.query.dimensions");
      expect(wire).toContain("VECTOR_DB");
      expect(wire).not.toMatch(/secret-|0\.123456789/);
      const spans = bodies.flatMap((body) => JSON.parse(body).resourceSpans.flatMap((resource: any) => resource.scopeSpans.flatMap((scope: any) => scope.spans)));
      expect(spans.map((span: any) => span.name)).toEqual(["mongodb aggregate", "mongodb find"]);
      expect(spans[1].status.code).toBe(2);
    } finally {
      await wireProvider.shutdown();
      await new Promise<void>((resolve) => receiver.close(() => resolve()));
    }
  });
});
