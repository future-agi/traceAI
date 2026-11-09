import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { context } from "@opentelemetry/api";
import { LanceDBInstrumentation } from "../instrumentation";

const manager = new AsyncLocalStorageContextManager();
beforeAll(() => context.setGlobalContextManager(manager.enable()));
afterAll(() => { context.disable(); });

for (const packageName of ["lancedb-floor", "@lancedb/lancedb"]) {
  describe(packageName, () => {
    const lance = require(packageName);
    let dir: string;
    let instrumentation: LanceDBInstrumentation;
    let exporter: InMemorySpanExporter;
    let provider: BasicTracerProvider;
    let db: any;

    beforeEach(async () => {
      dir = await mkdtemp(join(tmpdir(), "traceai-lancedb-"));
      exporter = new InMemorySpanExporter();
      provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      instrumentation = new LanceDBInstrumentation();
      instrumentation.setTracerProvider(provider);
      instrumentation.manuallyInstrument(lance);
      db = await lance.connect(dir);
    });
    afterEach(async () => {
      instrumentation.disable();
      db?.close();
      await provider.shutdown();
      await rm(dir, { recursive: true, force: true });
    });

    it("traces concrete table operations and preserves fluent search", async () => {
      const table = await db.createTable("vectors", [
        { id: 1, vector: [1, 0], text: "private record" },
        { id: 2, vector: [0, 1], text: "private record" },
      ]);
      exporter.reset();
      expect(await table.countRows()).toBe(2);
      expect(exporter.getFinishedSpans().map(span => span.name)).toEqual(["lancedb countRows"]);
      exporter.reset();
      const query = (table.search ? table.search([1, 0]) : table.vectorSearch([1, 0])).limit(1);
      expect(query).not.toBeInstanceOf(Promise);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      const rows = await query.toArray();
      expect(rows).toHaveLength(1);
      expect(exporter.getFinishedSpans().map(span => span.name)).toEqual(["lancedb search"]);
      expect(exporter.getFinishedSpans()[0].attributes).toMatchObject({
        "db.collection.name": "vectors", "db.vector.query.top_k": 1,
        "db.vector.query.dimensions": 2, "db.response.returned_rows": 1,
      });
      exporter.reset();
      expect((await query.toArrow()).numRows).toBe(1);
      expect((await query.toArray()).length).toBe(1);
      expect(exporter.getFinishedSpans()).toHaveLength(2);
    });

    it("covers connection and table mutations without capturing arguments", async () => {
      const table = await db.createTable("vectors", [{ id: 1, vector: [1, 0] }]);
      await db.openTable("vectors");
      expect(await db.tableNames()).toContain("vectors");
      await table.add([{ id: 2, vector: [0, 1] }]);
      await table.update({ id: "3" }, { where: "id = 2" });
      await table.delete("id = 3");
      expect(await table.countRows()).toBe(1);
      await db.dropTable("vectors");
      expect(exporter.getFinishedSpans().map(span => span.name)).toEqual([
        "lancedb createTable", "lancedb openTable", "lancedb tableNames", "lancedb add",
        "lancedb update", "lancedb delete", "lancedb countRows", "lancedb dropTable",
      ]);
      expect(JSON.stringify(exporter.getFinishedSpans().map(span => span.attributes))).not.toContain("id = 3");
    });

    it("traces queries that create another builder and both iterator entry points", async () => {
      const table = await db.createTable("vectors", [{ id: 1, vector: [1, 0] }]);
      exporter.reset();
      const query = table.query().nearestTo([1, 0]).limit(1);
      await query.toArray();
      expect(exporter.getFinishedSpans()[0].attributes["db.collection.name"]).toBe("vectors");
      exporter.reset();
      const iterator = query.execute();
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      let batch;
      do { batch = await iterator.next(); } while (!batch.done);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      exporter.reset();
      for await (const batch of query) {
        expect(batch.numRows).toBe(1);
        break;
      }
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    it("handles empty results and independent concurrent executions", async () => {
      const table = await db.createTable("vectors", [{ id: 1, vector: [1, 0] }]);
      exporter.reset();
      const query = table.query().where("id = -1");
      expect(await query.toArray()).toHaveLength(0);
      expect(exporter.getFinishedSpans()[0].attributes["db.response.returned_rows"]).toBe(0);
      exporter.reset();
      const results = await Promise.all([table.query().toArray(), table.query().toArray()]);
      expect(results.map(rows => rows.length)).toEqual([1, 1]);
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(2);
      expect(spans[0].spanContext().spanId).not.toBe(spans[1].spanContext().spanId);
    });

    it("records native execution failures and preserves enable/disable", async () => {
      const table = await db.createTable("vectors", [{ id: 1, vector: [1, 0] }]);
      exporter.reset();
      await expect(table.query().where("missing_column = 1").toArray()).rejects.toThrow();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(2);
      instrumentation.disable();
      exporter.reset();
      await table.countRows();
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      instrumentation.enable();
      await table.countRows();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });
  });
}
