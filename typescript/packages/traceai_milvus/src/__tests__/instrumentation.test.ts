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
    DB_COLLECTION_NAME: "db.collection.name",
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

let MilvusInstrumentation: any;
let isPatched: any;

describe("Milvus Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
    const mod = require("../instrumentation");
    MilvusInstrumentation = mod.MilvusInstrumentation;
    isPatched = mod.isPatched;
    instrumentation = new MilvusInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation?.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(MilvusInstrumentation);
    });

    it("should initialize with default config", () => {
      const inst = new MilvusInstrumentation();
      expect(inst).toBeInstanceOf(MilvusInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/milvus");
    });

    it("should accept custom configuration", () => {
      const inst = new MilvusInstrumentation({
        instrumentationConfig: { captureQueryVectors: true },
        traceConfig: { maskInputs: true },
      });
      expect(inst).toBeInstanceOf(MilvusInstrumentation);
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch MilvusClient methods", () => {
      const mockModule = {
        MilvusClient: {
          prototype: {
            search: jest.fn(),
            query: jest.fn(),
            insert: jest.fn(),
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);
      expect(isPatched()).toBe(true);
    });

    it("should not double-patch", () => {
      const mockModule = {
        MilvusClient: { prototype: { search: jest.fn() } },
      };
      instrumentation.manuallyInstrument(mockModule);
      const first = mockModule.MilvusClient.prototype.search;
      instrumentation.manuallyInstrument(mockModule);
      expect(mockModule.MilvusClient.prototype.search).toBe(first);
    });
  });

  describe("Search Operations", () => {
    it("should create span for search operation", async () => {
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue({ results: [] }),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.MilvusClient.prototype.search.call(mockClient, {
        collection_name: "test",
        limit: 10,
      });

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle search with vector input", async () => {
      const queryVector = new Array(128).fill(0).map(() => Math.random());
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue({
          results: [
            { id: 1, score: 0.95, entity: { name: "Item 1" } },
            { id: 2, score: 0.87, entity: { name: "Item 2" } },
          ],
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.MilvusClient.prototype.search.call(mockClient, {
        collection_name: "products",
        vector: queryVector,
        limit: 10,
        output_fields: ["name", "price"],
      });

      expect(result.results).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle search with filters", async () => {
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue({ results: [] }),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.MilvusClient.prototype.search.call(mockClient, {
        collection_name: "products",
        vector: [0.1, 0.2, 0.3],
        limit: 20,
        filter: 'category == "electronics" && price < 1000',
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Query Operations", () => {
    it("should trace query operation", async () => {
      const mockClient = {
        query: (jest.fn() as MockFn).mockResolvedValue({
          data: [{ id: 1, name: "Test" }],
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { query: mockClient.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.MilvusClient.prototype.query.call(mockClient, {
        collection_name: "items",
        filter: "id in [1, 2, 3]",
        output_fields: ["name", "price"],
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Data Operations", () => {
    it("should trace insert operation", async () => {
      const mockClient = {
        insert: (jest.fn() as MockFn).mockResolvedValue({
          insert_cnt: 100,
          ids: Array.from({ length: 100 }, (_, i) => i),
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { insert: mockClient.insert } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const data = Array.from({ length: 100 }, (_, i) => ({
        id: i,
        vector: new Array(128).fill(0),
        name: `Item ${i}`,
      }));

      const result = await mockModule.MilvusClient.prototype.insert.call(mockClient, {
        collection_name: "items",
        data,
      });

      expect(result.insert_cnt).toBe(100);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace upsert operation", async () => {
      const mockClient = {
        upsert: (jest.fn() as MockFn).mockResolvedValue({
          upsert_cnt: 50,
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { upsert: mockClient.upsert } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.MilvusClient.prototype.upsert.call(mockClient, {
        collection_name: "items",
        data: [{ id: 1, vector: [0.1, 0.2], name: "Updated" }],
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace delete operation", async () => {
      const mockClient = {
        delete: (jest.fn() as MockFn).mockResolvedValue({
          delete_cnt: 5,
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { delete: mockClient.delete } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.MilvusClient.prototype.delete.call(mockClient, {
        collection_name: "items",
        filter: "id in [1, 2, 3, 4, 5]",
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection Operations", () => {
    it("should trace createCollection operation", async () => {
      const mockClient = {
        createCollection: (jest.fn() as MockFn).mockResolvedValue({ code: 0 }),
      };
      const mockModule = {
        MilvusClient: { prototype: { createCollection: mockClient.createCollection } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.MilvusClient.prototype.createCollection.call(mockClient, {
        collection_name: "new_collection",
        dimension: 128,
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

  });

  describe("Error Handling", () => {
    it("should record exception on error", async () => {
      const error = new Error("Search failed");
      const mockClient = {
        search: (jest.fn() as MockFn).mockRejectedValue(error),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.MilvusClient.prototype.search.call(mockClient, {})
      ).rejects.toThrow("Search failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle connection timeout", async () => {
      const timeoutError = new Error("Connection timeout");
      const mockClient = {
        search: (jest.fn() as MockFn).mockRejectedValue(timeoutError),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.MilvusClient.prototype.search.call(mockClient, {})
      ).rejects.toThrow("Connection timeout");

      expect(mockSpan.recordException).toHaveBeenCalled();
    });

    it("should handle collection not found error", async () => {
      const notFoundError = new Error("Collection not found: missing_collection");
      const mockClient = {
        search: (jest.fn() as MockFn).mockRejectedValue(notFoundError),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.MilvusClient.prototype.search.call(mockClient, {
          collection_name: "missing_collection",
        })
      ).rejects.toThrow("Collection not found");

      expect(mockSpan.recordException).toHaveBeenCalled();
    });
  });

  describe("Real-World Scenarios", () => {
    it("should handle image similarity search", async () => {
      const imageEmbedding = new Array(512).fill(0).map(() => Math.random());
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue({
          results: [
            { id: 1, score: 0.98, entity: { filename: "cat1.jpg", category: "pets" } },
            { id: 2, score: 0.94, entity: { filename: "cat2.jpg", category: "pets" } },
          ],
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.MilvusClient.prototype.search.call(mockClient, {
        collection_name: "images",
        vector: imageEmbedding,
        limit: 10,
        output_fields: ["filename", "category"],
        params: { nprobe: 16 },
      });

      expect(result.results).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle fraud detection pattern matching", async () => {
      const transactionVector = new Array(64).fill(0).map(() => Math.random());
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue({
          results: [
            { id: 1, score: 0.92, entity: { pattern_type: "fraud", risk_score: 0.85 } },
          ],
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.MilvusClient.prototype.search.call(mockClient, {
        collection_name: "fraud_patterns",
        vector: transactionVector,
        limit: 5,
        params: { nprobe: 32 },
      });

      const fraudMatches = result.results.filter(
        (r: any) => r.entity.pattern_type === "fraud" && r.score > 0.85
      );
      expect(fraudMatches).toHaveLength(1);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle batch vector ingestion", async () => {
      const batchSize = 1000;
      const vectors = Array.from({ length: batchSize }, (_, i) => ({
        id: i,
        vector: new Array(128).fill(0),
        metadata: JSON.stringify({ source: "batch", index: i }),
      }));

      const mockClient = {
        insert: (jest.fn() as MockFn).mockResolvedValue({
          insert_cnt: batchSize,
          ids: vectors.map((v) => v.id),
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { insert: mockClient.insert } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.MilvusClient.prototype.insert.call(mockClient, {
        collection_name: "documents",
        data: vectors,
      });

      expect(result.insert_cnt).toBe(batchSize);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle hybrid filtering with scalar fields", async () => {
      const queryVector = new Array(128).fill(0).map(() => Math.random());
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue({
          results: [
            { id: 1, score: 0.95, entity: { name: "Product A", price: 299 } },
            { id: 2, score: 0.89, entity: { name: "Product B", price: 199 } },
          ],
        }),
      };
      const mockModule = {
        MilvusClient: { prototype: { search: mockClient.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.MilvusClient.prototype.search.call(mockClient, {
        collection_name: "products",
        vector: queryVector,
        limit: 100,
        filter: 'category in ["electronics", "computers"] && price >= 100 && price <= 500 && in_stock == true',
        output_fields: ["name", "price", "category", "in_stock", "rating"],
      });

      expect(result.results).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
