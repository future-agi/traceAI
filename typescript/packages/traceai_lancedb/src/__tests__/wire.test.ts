import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { LanceDBInstrumentation } from "../instrumentation";

beforeAll(() => context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()));
afterAll(() => context.disable());

for (const packageName of ["lancedb-floor", "@lancedb/lancedb"]) {
  describe(`${packageName} loopback cloud transport and OTLP`, () => {
    let server: Server;
    let provider: BasicTracerProvider;
    let instrumentation: LanceDBInstrumentation;
    let db: any;
    let dir: string | undefined;
    const payloads: any[] = [];
    const requests: string[] = [];

    afterEach(async () => {
      instrumentation?.disable();
      db?.close();
      await provider?.shutdown();
      await new Promise<void>(resolve => server.close(() => resolve()));
      if (dir) await rm(dir, { recursive: true, force: true });
    });

    it("exports database spans, parenting and private error policy over HTTP", async () => {
      payloads.length = 0;
      requests.length = 0;
      let failCount = false;
      server = createServer(async (req, res) => {
        const chunks: Buffer[] = [];
        for await (const chunk of req) chunks.push(Buffer.from(chunk));
        const body = Buffer.concat(chunks).toString();
        res.setHeader("Content-Type", "application/json");
        if (req.url === "/v1/traces") {
          payloads.push(JSON.parse(body));
          res.end("{}");
        } else {
          requests.push(req.url!);
          if (req.url?.includes("describe")) res.end(JSON.stringify({ schema: { fields: [] }, version: 1 }));
          else if (req.url?.includes("count_rows")) {
            if (failCount) { res.statusCode = 400; res.end("private_engine_failure"); }
            else res.end("2");
          } else if (req.url?.startsWith("/v1/table/")) res.end(JSON.stringify({ tables: ["vectors"] }));
          else { res.statusCode = 404; res.end("{}"); }
        }
      });
      await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
      const host = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
      const exporter = new OTLPTraceExporter({ url: `${host}/v1/traces` });
      provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      instrumentation = new LanceDBInstrumentation({ traceConfig: { hideErrorDetails: true } });
      instrumentation.setTracerProvider(provider);
      const lance = require(packageName);
      instrumentation.manuallyInstrument(lance);
      // These are fabricated credentials for the local test server, never vendor credentials.
      if (packageName === "lancedb-floor") {
        // The 0.4.20 native binary was built without the remote feature.
        dir = await mkdtemp(join(tmpdir(), "traceai-lancedb-wire-"));
        db = await lance.connect(dir);
        await db.createTable("vectors", [{ id: 1, vector: [1, 0] }, { id: 2, vector: [0, 1] }]);
        await provider.forceFlush();
        payloads.length = 0;
      } else {
        db = await lance.connect("db://synthetic", { apiKey: "synthetic-test-only", region: "us-east-1", hostOverride: host });
      }
      const parent = provider.getTracer("test").startSpan("parent");
      await context.with(trace.setSpan(context.active(), parent), async () => {
        expect(await db.tableNames()).toEqual(["vectors"]);
        const table = await db.openTable("vectors");
        expect(await table.countRows()).toBe(2);
        failCount = true;
        await expect(table.countRows("private_engine_failure = 1")).rejects.toThrow();
      });
      parent.end();
      await provider.forceFlush();
      const spans = payloads.flatMap(payload => payload.resourceSpans.flatMap((resource: any) =>
        resource.scopeSpans.flatMap((scope: any) => scope.spans)));
      const database = spans.filter((span: any) => span.name.startsWith("lancedb"));
      expect(database.map((span: any) => span.name)).toEqual([
        "lancedb tableNames", "lancedb openTable", "lancedb countRows", "lancedb countRows",
      ]);
      for (const span of database) {
        expect(span.parentSpanId).toBe(parent.spanContext().spanId);
        expect(span.kind).toBe(3); // OTLP CLIENT enum
        expect(BigInt(span.endTimeUnixNano)).toBeGreaterThanOrEqual(BigInt(span.startTimeUnixNano));
        expect(span.attributes).toEqual(expect.arrayContaining([
          { key: "db.system", value: { stringValue: "lancedb" } },
          { key: "fi.span.kind", value: { stringValue: "VECTOR_DB" } },
        ]));
      }
      expect(database[3].status.code).toBe(2);
      expect(JSON.stringify(payloads)).not.toContain("private_engine_failure");
      expect(JSON.stringify(payloads)).not.toContain("synthetic-test-only");
      expect(JSON.stringify(payloads)).not.toContain("db://synthetic");
      if (packageName !== "lancedb-floor") expect(requests.some(path => path.includes("count_rows"))).toBe(true);
    });
  });
}
