import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";

type MockFn = jest.Mock<any>;

// Mock span for testing
const createMockSpan = () => ({
  setStatus: jest.fn(),
  setAttribute: jest.fn(),
  recordException: jest.fn(),
  setAttributes: jest.fn(),
  addEvent: jest.fn(),
  end: jest.fn(),
  isRecording: jest.fn().mockReturnValue(true),
  getSpanContext: jest.fn().mockReturnValue({
    traceId: "mock-trace-id",
    spanId: "mock-span-id",
    traceFlags: 1,
  }),
});

// Mock FITracer
const mockSpan = createMockSpan();
jest.mock("@traceai/fi-core", () => ({
  FITracer: jest.fn().mockImplementation(() => ({
    startSpan: jest.fn().mockReturnValue(mockSpan),
    startActiveSpan: jest.fn((name: any, fn: any) => fn(mockSpan)),
  })),
  TraceConfigOptions: {},
}));

// Mock OpenTelemetry context
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
      debug: jest.fn(),
      info: jest.fn(),
      warn: jest.fn(),
      error: jest.fn(),
      verbose: jest.fn(),
    }),
    debug: jest.fn(),
    info: jest.fn(),
    warn: jest.fn(),
    error: jest.fn(),
    verbose: jest.fn(),
  },
  SpanKind: {
    CLIENT: 2,
  },
  SpanStatusCode: {
    OK: 1,
    ERROR: 2,
  },
}));

// Import after mocks
let PineconeInstrumentation: any;
let isPatched: any;
let FITracer: any;

describe("Pinecone Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();

    const instrumentationModule = require("../instrumentation");
    PineconeInstrumentation = instrumentationModule.PineconeInstrumentation;
    isPatched = instrumentationModule.isPatched;
    FITracer = require("@traceai/fi-core").FITracer;

    instrumentation = new PineconeInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
      traceConfig: {
        maskInputs: false,
        maskOutputs: false,
      },
    });

    instrumentation.enable();
  });

  afterEach(() => {
    if (instrumentation) {
      instrumentation.disable();
    }
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(PineconeInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new PineconeInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(PineconeInstrumentation);
    });

    it("should initialize with custom config", () => {
      const customInstrumentation = new PineconeInstrumentation({
        instrumentationConfig: {
          enabled: true,
          captureQueryVectors: true,
          captureResultVectors: false,
        },
        traceConfig: {
          maskInputs: true,
          maskOutputs: true,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(PineconeInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/pinecone");
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch Index class methods", () => {
      const mockIndex = {
        Index: {
          prototype: {
            query: jest.fn(),
            upsert: jest.fn(),
            fetch: jest.fn(),
            update: jest.fn(),
            deleteOne: jest.fn(),
            deleteMany: jest.fn(),
            deleteAll: jest.fn(),
            listPaginated: jest.fn(),
            describeIndexStats: jest.fn(),
          },
        },
      };

      instrumentation.manuallyInstrument(mockIndex);

      expect(isPatched()).toBe(true);
      expect(mockIndex.Index.prototype.query).toBeDefined();
      expect(mockIndex.Index.prototype.upsert).toBeDefined();
    });

    it("should not double-patch", () => {
      const mockModule = {
        Index: {
          prototype: {
            query: jest.fn(),
            upsert: jest.fn(),
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);
      const firstQuery = mockModule.Index.prototype.query;

      instrumentation.manuallyInstrument(mockModule);
      const secondQuery = mockModule.Index.prototype.query;

      expect(firstQuery).toBe(secondQuery);
      expect(isPatched()).toBe(true);
    });
  });

  describe("Index.query Operation", () => {
    it("should create span with correct attributes for query operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        namespace: "test_namespace",
        query: (jest.fn() as MockFn).mockResolvedValue({
          matches: [
            { id: "vec1", score: 0.95 },
            { id: "vec2", score: 0.87 },
          ],
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            query: mockIndex.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        vector: new Array(1536).fill(0).map(() => Math.random()),
        topK: 10,
        filter: { category: "electronics" },
        includeMetadata: true,
        includeValues: false,
      };

      await mockModule.Index.prototype.query.call(mockIndex, params);

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Index.upsert Operation", () => {
    it("should create span for upsert operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        upsert: (jest.fn() as MockFn).mockResolvedValue({ upsertedCount: 3 }),
      };

      const mockModule = {
        Index: {
          prototype: {
            upsert: mockIndex.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const vectors = [
        { id: "vec1", values: new Array(1536).fill(0.1), metadata: { key: "value1" } },
        { id: "vec2", values: new Array(1536).fill(0.2), metadata: { key: "value2" } },
        { id: "vec3", values: new Array(1536).fill(0.3), metadata: { key: "value3" } },
      ];

      await mockModule.Index.prototype.upsert.call(mockIndex, vectors);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Index.fetch Operation", () => {
    it("should create span for fetch operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        fetch: (jest.fn() as MockFn).mockResolvedValue({
          records: {
            vec1: { id: "vec1", values: [0.1, 0.2] },
            vec2: { id: "vec2", values: [0.3, 0.4] },
          },
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            fetch: mockIndex.fetch,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const ids = ["vec1", "vec2"];

      await mockModule.Index.prototype.fetch.call(mockIndex, ids);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Index.deleteOne Operation", () => {
    it("should create span for deleteOne operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        deleteOne: (jest.fn() as MockFn).mockResolvedValue({}),
      };

      const mockModule = {
        Index: {
          prototype: {
            deleteOne: mockIndex.deleteOne,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Index.prototype.deleteOne.call(mockIndex, "vec1");

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Index.deleteMany Operation", () => {
    it("should create span for deleteMany operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        deleteMany: (jest.fn() as MockFn).mockResolvedValue({}),
      };

      const mockModule = {
        Index: {
          prototype: {
            deleteMany: mockIndex.deleteMany,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Index.prototype.deleteMany.call(mockIndex, ["vec1", "vec2", "vec3"]);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Index.deleteAll Operation", () => {
    it("should create span for deleteAll operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        deleteAll: (jest.fn() as MockFn).mockResolvedValue({}),
      };

      const mockModule = {
        Index: {
          prototype: {
            deleteAll: mockIndex.deleteAll,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Index.prototype.deleteAll.call(mockIndex);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Index.describeIndexStats Operation", () => {
    it("should create span for describeIndexStats operation", async () => {
      const mockIndex = {
        indexName: "test_index",
        describeIndexStats: (jest.fn() as MockFn).mockResolvedValue({
          totalRecordCount: 10000,
          dimension: 1536,
          namespaces: {
            "": { recordCount: 5000 },
            "ns1": { recordCount: 5000 },
          },
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            describeIndexStats: mockIndex.describeIndexStats,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Index.prototype.describeIndexStats.call(mockIndex);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on query error", async () => {
      const error = new Error("Query failed: rate limit exceeded");
      const mockIndex = {
        indexName: "test_index",
        query: (jest.fn() as MockFn).mockRejectedValue(error),
      };

      const mockModule = {
        Index: {
          prototype: {
            query: mockIndex.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Index.prototype.query.call(mockIndex, { vector: [0.1] })
      ).rejects.toThrow("Query failed: rate limit exceeded");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.setStatus).toHaveBeenCalledWith({
        code: 2,
        message: "Query failed: rate limit exceeded",
      });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should record exception on upsert error", async () => {
      const error = new Error("Upsert failed: invalid dimension");
      const mockIndex = {
        indexName: "test_index",
        upsert: (jest.fn() as MockFn).mockRejectedValue(error),
      };

      const mockModule = {
        Index: {
          prototype: {
            upsert: mockIndex.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Index.prototype.upsert.call(mockIndex, [{ id: "vec1", values: [0.1] }])
      ).rejects.toThrow("Upsert failed: invalid dimension");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});

describe("Pinecone Instrumentation - Real World Scenarios", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();

    const { PineconeInstrumentation } = require("../instrumentation");

    instrumentation = new PineconeInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    if (instrumentation) {
      instrumentation.disable();
    }
  });

  describe("Serverless RAG Pipeline", () => {
    it("should trace high-scale document retrieval", async () => {
      const mockIndex = {
        indexName: "production_index",
        namespace: "documents",
        query: (jest.fn() as MockFn).mockResolvedValue({
          matches: [
            { id: "doc_1", score: 0.95, metadata: { title: "API Guide" } },
            { id: "doc_2", score: 0.91, metadata: { title: "SDK Reference" } },
            { id: "doc_3", score: 0.88, metadata: { title: "Best Practices" } },
          ],
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            query: mockIndex.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Simulate serverless retrieval with namespace
      const queryParams = {
        vector: new Array(1536).fill(0).map(() => Math.random()),
        topK: 5,
        namespace: "documents",
        includeMetadata: true,
        filter: {
          category: { $in: ["documentation", "guides"] },
          access_level: { $eq: "public" },
        },
      };

      const result = await mockModule.Index.prototype.query.call(mockIndex, queryParams);

      expect(result.matches).toHaveLength(3);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Multi-tenant Search", () => {
    it("should trace namespace-isolated queries", async () => {
      const mockIndex = {
        indexName: "multi_tenant_index",
        query: (jest.fn() as MockFn).mockResolvedValue({
          matches: [{ id: "item_1", score: 0.92 }],
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            query: mockIndex.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Query with tenant namespace isolation
      const queryParams = {
        vector: new Array(768).fill(0).map(() => Math.random()),
        topK: 10,
        namespace: "tenant_abc123", // Tenant isolation via namespace
        includeMetadata: true,
      };

      await mockModule.Index.prototype.query.call(mockIndex, queryParams);

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace tenant data ingestion", async () => {
      const mockIndex = {
        indexName: "multi_tenant_index",
        namespace: "tenant_xyz789",
        upsert: (jest.fn() as MockFn).mockResolvedValue({ upsertedCount: 100 }),
      };

      const mockModule = {
        Index: {
          prototype: {
            upsert: mockIndex.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Batch upsert for tenant
      const vectors = Array.from({ length: 100 }, (_, i) => ({
        id: `tenant_xyz789_item_${i}`,
        values: new Array(768).fill(0).map(() => Math.random()),
        metadata: {
          tenant_id: "xyz789",
          item_type: "product",
          created_at: new Date().toISOString(),
        },
      }));

      await mockModule.Index.prototype.upsert.call(mockIndex, vectors);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Recommendation Engine", () => {
    it("should trace similar item lookup", async () => {
      const mockIndex = {
        indexName: "product_embeddings",
        query: (jest.fn() as MockFn).mockResolvedValue({
          matches: [
            { id: "prod_456", score: 0.94, metadata: { name: "Similar Product 1", price: 29.99 } },
            { id: "prod_789", score: 0.89, metadata: { name: "Similar Product 2", price: 34.99 } },
          ],
        }),
        fetch: (jest.fn() as MockFn).mockResolvedValue({
          records: {
            prod_123: {
              id: "prod_123",
              values: new Array(512).fill(0.1),
              metadata: { name: "Source Product" },
            },
          },
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            query: mockIndex.query,
            fetch: mockIndex.fetch,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Fetch the source product embedding
      const sourceProduct = await mockModule.Index.prototype.fetch.call(mockIndex, ["prod_123"]);

      // Find similar products
      const queryParams = {
        vector: sourceProduct.records.prod_123.values,
        topK: 10,
        filter: {
          category: { $eq: "electronics" },
          in_stock: { $eq: true },
        },
        includeMetadata: true,
      };

      const recommendations = await mockModule.Index.prototype.query.call(mockIndex, queryParams);

      expect(recommendations.matches.length).toBeGreaterThan(0);
      expect(mockSpan.end).toHaveBeenCalledTimes(2);
    });
  });

  describe("Index Maintenance", () => {
    it("should trace index statistics retrieval", async () => {
      const mockIndex = {
        indexName: "production_index",
        describeIndexStats: (jest.fn() as MockFn).mockResolvedValue({
          totalRecordCount: 1500000,
          dimension: 1536,
          indexFullness: 0.75,
          namespaces: {
            "documents": { recordCount: 500000 },
            "products": { recordCount: 750000 },
            "users": { recordCount: 250000 },
          },
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            describeIndexStats: mockIndex.describeIndexStats,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const stats = await mockModule.Index.prototype.describeIndexStats.call(mockIndex);

      expect(stats.totalRecordCount).toBe(1500000);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace namespace cleanup", async () => {
      const mockIndex = {
        indexName: "production_index",
        deleteAll: (jest.fn() as MockFn).mockResolvedValue({}),
      };

      const mockModule = {
        Index: {
          prototype: {
            deleteAll: mockIndex.deleteAll,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Clear a namespace (e.g., for data refresh)
      await mockModule.Index.prototype.deleteAll.call(mockIndex);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Hybrid Search Scenario", () => {
    it("should trace filtered semantic search", async () => {
      const mockIndex = {
        indexName: "ecommerce_index",
        query: (jest.fn() as MockFn).mockResolvedValue({
          matches: [
            {
              id: "item_001",
              score: 0.96,
              metadata: {
                title: "Wireless Headphones",
                brand: "Sony",
                price: 149.99,
                rating: 4.5,
              },
            },
          ],
        }),
      };

      const mockModule = {
        Index: {
          prototype: {
            query: mockIndex.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Hybrid search: semantic similarity + metadata filters
      const queryParams = {
        vector: new Array(1536).fill(0).map(() => Math.random()),
        topK: 20,
        filter: {
          $and: [
            { price: { $lte: 200 } },
            { rating: { $gte: 4.0 } },
            { brand: { $in: ["Sony", "Bose", "Apple"] } },
          ],
        },
        includeMetadata: true,
      };

      await mockModule.Index.prototype.query.call(mockIndex, queryParams);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
