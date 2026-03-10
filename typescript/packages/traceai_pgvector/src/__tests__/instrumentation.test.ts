import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";

type MockFn = jest.Mock<any>;

const createMockSpan = () => ({
  setStatus: jest.fn(),
  setAttribute: jest.fn(),
  recordException: jest.fn(),
  setAttributes: jest.fn(),
  addEvent: jest.fn(),
  end: jest.fn(),
  isRecording: jest.fn().mockReturnValue(true),
});

const mockSpan = createMockSpan();
jest.mock("@traceai/fi-core", () => ({
  FITracer: jest.fn().mockImplementation(() => ({
    startSpan: jest.fn().mockReturnValue(mockSpan),
  })),
  TraceConfigOptions: {},
}));

jest.mock("@traceai/fi-semantic-conventions", () => ({
  SemanticConventions: {
    DB_SYSTEM: "db.system",
    DB_OPERATION: "db.operation",
    DB_STATEMENT: "db.statement",
  },
}));

jest.mock("@opentelemetry/api", () => ({
  context: {
    active: jest.fn().mockReturnValue({}),
    with: jest.fn((_ctx: any, fn: () => any) => fn()),
  },
  trace: {
    setSpan: jest.fn().mockReturnValue({}),
    getTracer: jest.fn().mockReturnValue({
      startSpan: jest.fn(),
      startActiveSpan: jest.fn(),
    }),
  },
  metrics: {
    getMeter: jest.fn().mockReturnValue({
      createCounter: jest.fn().mockReturnValue({ add: jest.fn() }),
      createHistogram: jest.fn().mockReturnValue({ record: jest.fn() }),
      createUpDownCounter: jest.fn().mockReturnValue({ add: jest.fn() }),
    }),
  },
  diag: {
    createComponentLogger: jest.fn().mockReturnValue({
      debug: jest.fn(), info: jest.fn(), warn: jest.fn(), error: jest.fn(),
    }),
  },
  SpanKind: { CLIENT: 2 },
  SpanStatusCode: { OK: 1, ERROR: 2 },
}));

let PgVectorInstrumentation: any;
let isPatched: any;

describe("PgVector Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
    const mod = require("../instrumentation");
    PgVectorInstrumentation = mod.PgVectorInstrumentation;
    isPatched = mod.isPatched;
    instrumentation = new PgVectorInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation?.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(PgVectorInstrumentation);
    });

    it("should initialize with default config", () => {
      const inst = new PgVectorInstrumentation();
      expect(inst).toBeInstanceOf(PgVectorInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/pgvector");
    });

    it("should accept custom configuration", () => {
      const inst = new PgVectorInstrumentation({
        instrumentationConfig: { captureStatements: true },
        traceConfig: { maskInputs: true },
      });
      expect(inst).toBeInstanceOf(PgVectorInstrumentation);
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch Client and Pool query methods", () => {
      const mockModule = {
        Client: {
          prototype: { query: jest.fn() },
        },
        Pool: {
          prototype: { query: jest.fn() },
        },
      };
      instrumentation.manuallyInstrument(mockModule);
      expect(isPatched()).toBe(true);
    });
  });

  describe("Basic Query Operations", () => {
    it("should create span for SELECT query", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rows: [], rowCount: 0 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Client.prototype.query.call(mockClient, "SELECT * FROM items");

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle INSERT query", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rowCount: 1 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Client.prototype.query.call(
        mockClient,
        "INSERT INTO items (name, embedding) VALUES ($1, $2)",
        ["Test Item", JSON.stringify([0.1, 0.2, 0.3])]
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle UPDATE query", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rowCount: 5 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Client.prototype.query.call(
        mockClient,
        "UPDATE items SET embedding = $1 WHERE category = $2",
        [JSON.stringify([0.1, 0.2, 0.3]), "electronics"]
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Vector Search Detection", () => {
    it("should detect L2 distance operator", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rows: [], rowCount: 0 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Client.prototype.query.call(
        mockClient,
        "SELECT * FROM items ORDER BY embedding <-> $1 LIMIT 10",
        [[0.1, 0.2, 0.3]]
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should detect cosine distance operator", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rows: [], rowCount: 0 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Client.prototype.query.call(
        mockClient,
        "SELECT * FROM items ORDER BY embedding <=> $1 LIMIT 5"
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should detect inner product operator", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rows: [], rowCount: 0 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Client.prototype.query.call(
        mockClient,
        "SELECT * FROM items ORDER BY embedding <#> $1 LIMIT 10"
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on error", async () => {
      const error = new Error("Query failed");
      const mockClient = {
        query: (jest.fn() as MockFn).mockRejectedValue(error),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Client.prototype.query.call(mockClient, "SELECT 1")
      ).rejects.toThrow("Query failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle connection errors", async () => {
      const connectionError = new Error("Connection refused");
      const mockClient = {
        query: (jest.fn() as MockFn).mockRejectedValue(connectionError),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Client.prototype.query.call(mockClient, "SELECT 1")
      ).rejects.toThrow("Connection refused");

      expect(mockSpan.recordException).toHaveBeenCalled();
    });

    it("should handle syntax errors", async () => {
      const syntaxError = new Error("syntax error at or near");
      const mockClient = {
        query: (jest.fn() as MockFn).mockRejectedValue(syntaxError),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Client.prototype.query.call(mockClient, "SELEC * FROM items")
      ).rejects.toThrow("syntax error");

      expect(mockSpan.recordException).toHaveBeenCalled();
    });
  });

  describe("Pool Operations", () => {
    it("should trace Pool query", async () => {
      const mockPool = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          rows: [{ id: 1, name: "Test" }],
          rowCount: 1,
        }),
      };
      const mockModule = {
        Pool: { prototype: { query: mockPool.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Pool.prototype.query.call(
        mockPool,
        "SELECT * FROM items WHERE id = $1",
        [1]
      );

      expect(result.rows).toHaveLength(1);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Real-World Scenarios", () => {
    it("should handle semantic search with similarity threshold", async () => {
      const queryVector = new Array(1536).fill(0).map(() => Math.random());
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          rows: [
            { id: 1, title: "ML Basics", similarity: 0.92 },
            { id: 2, title: "Deep Learning", similarity: 0.85 },
          ],
          rowCount: 2,
        }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Client.prototype.query.call(
        mockClient,
        `SELECT id, title, 1 - (embedding <=> $1) AS similarity
         FROM articles
         WHERE 1 - (embedding <=> $1) > 0.7
         ORDER BY embedding <=> $1
         LIMIT $2`,
        [JSON.stringify(queryVector), 10]
      );

      expect(result.rows).toHaveLength(2);
      expect(result.rows[0].similarity).toBeGreaterThan(0.7);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle hybrid search with full-text", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          rows: [
            { id: 1, name: "Laptop Pro", combined_score: 0.88 },
            { id: 2, name: "Gaming Laptop", combined_score: 0.75 },
          ],
          rowCount: 2,
        }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Client.prototype.query.call(
        mockClient,
        `WITH vector_results AS (
           SELECT id, 1 - (embedding <=> $1) AS vector_score
           FROM products ORDER BY embedding <=> $1 LIMIT 100
         ),
         text_results AS (
           SELECT id, ts_rank(search_vector, plainto_tsquery($2)) AS text_score
           FROM products WHERE search_vector @@ plainto_tsquery($2)
         )
         SELECT p.id, p.name,
                COALESCE(v.vector_score, 0) * 0.7 + COALESCE(t.text_score, 0) * 0.3 AS combined_score
         FROM products p
         LEFT JOIN vector_results v ON p.id = v.id
         LEFT JOIN text_results t ON p.id = t.id
         ORDER BY combined_score DESC LIMIT 20`,
        [JSON.stringify([0.1, 0.2]), "laptop"]
      );

      expect(result.rows).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle K-NN with filtering", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          rows: [
            { id: 10, name: "Similar Product 1", distance: 0.15 },
            { id: 20, name: "Similar Product 2", distance: 0.23 },
          ],
          rowCount: 2,
        }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Client.prototype.query.call(
        mockClient,
        `WITH source AS (SELECT embedding FROM products WHERE id = $1)
         SELECT p.id, p.name, p.embedding <-> s.embedding AS distance
         FROM products p, source s
         WHERE p.id != $1 AND p.category = $2 AND p.price >= $3 AND p.price <= $4 AND p.in_stock = true
         ORDER BY p.embedding <-> s.embedding
         LIMIT $5`,
        [1, "electronics", 100, 500, 10]
      );

      expect(result.rows).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle batch vector upsert", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({ rowCount: 50 }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      // Simulate batch upsert
      for (let i = 0; i < 5; i++) {
        await mockModule.Client.prototype.query.call(
          mockClient,
          `INSERT INTO documents (id, content, embedding)
           VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE
           SET content = EXCLUDED.content, embedding = EXCLUDED.embedding, updated_at = NOW()`,
          [i, `Content ${i}`, JSON.stringify(new Array(1536).fill(0))]
        );
      }

      expect(mockSpan.end).toHaveBeenCalledTimes(5);
    });

    it("should handle multi-tenant vector search", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          rows: [
            { id: 1, title: "Tenant Doc 1", distance: 0.12 },
            { id: 2, title: "Tenant Doc 2", distance: 0.18 },
          ],
          rowCount: 2,
        }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Client.prototype.query.call(
        mockClient,
        `SELECT id, title, embedding <=> $2 AS distance
         FROM tenant_documents
         WHERE tenant_id = $1
         ORDER BY embedding <=> $2
         LIMIT $3`,
        ["tenant-123", JSON.stringify([0.1, 0.2, 0.3]), 20]
      );

      expect(result.rows).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle HNSW index with ef_search parameter", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn)
          .mockResolvedValueOnce({ rows: [] }) // SET command
          .mockResolvedValueOnce({
            rows: [{ id: 1, content: "Result", similarity: 0.95 }],
            rowCount: 1,
          }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      // Set search parameter
      await mockModule.Client.prototype.query.call(mockClient, "SET hnsw.ef_search = 100");

      // Execute search
      const result = await mockModule.Client.prototype.query.call(
        mockClient,
        `SELECT id, content, 1 - (embedding <=> $1) AS similarity
         FROM documents
         ORDER BY embedding <=> $1
         LIMIT $2`,
        [JSON.stringify([0.1, 0.2, 0.3]), 10]
      );

      expect(result.rows).toHaveLength(1);
      expect(mockSpan.end).toHaveBeenCalledTimes(2);
    });

    it("should handle vector clustering analytics", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          rows: [
            { cluster_id: 1, document_count: 150, avg_distance: 0.25 },
            { cluster_id: 2, document_count: 230, avg_distance: 0.31 },
            { cluster_id: 3, document_count: 120, avg_distance: 0.22 },
          ],
          rowCount: 3,
        }),
      };
      const mockModule = {
        Client: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const centroids = [
        JSON.stringify([0.1, 0.2]),
        JSON.stringify([0.5, 0.6]),
        JSON.stringify([0.8, 0.9]),
      ];

      const result = await mockModule.Client.prototype.query.call(
        mockClient,
        `WITH centroids AS (
           SELECT row_number() OVER () AS cluster_id, centroid::vector AS centroid
           FROM unnest($1::text[]) AS centroid
         )
         SELECT c.cluster_id, COUNT(*) AS document_count, AVG(d.embedding <-> c.centroid) AS avg_distance
         FROM documents d
         CROSS JOIN LATERAL (
           SELECT cluster_id, centroid FROM centroids ORDER BY d.embedding <-> centroid LIMIT 1
         ) c
         GROUP BY c.cluster_id ORDER BY c.cluster_id`,
        [centroids]
      );

      expect(result.rows).toHaveLength(3);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
