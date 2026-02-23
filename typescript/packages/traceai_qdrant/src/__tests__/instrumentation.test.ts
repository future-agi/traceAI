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
let QdrantInstrumentation: any;
let isPatched: any;
let FITracer: any;

describe("Qdrant Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();

    const instrumentationModule = require("../instrumentation");
    QdrantInstrumentation = instrumentationModule.QdrantInstrumentation;
    isPatched = instrumentationModule.isPatched;
    FITracer = require("@traceai/fi-core").FITracer;

    instrumentation = new QdrantInstrumentation({
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
      expect(instrumentation).toBeInstanceOf(QdrantInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new QdrantInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(QdrantInstrumentation);
    });

    it("should initialize with custom config", () => {
      const customInstrumentation = new QdrantInstrumentation({
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
      expect(customInstrumentation).toBeInstanceOf(QdrantInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/qdrant");
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch QdrantClient class methods", () => {
      const mockClient = {
        QdrantClient: {
          prototype: {
            search: jest.fn(),
            query: jest.fn(),
            queryPoints: jest.fn(),
            upsert: jest.fn(),
            delete: jest.fn(),
            retrieve: jest.fn(),
            scroll: jest.fn(),
            count: jest.fn(),
            getCollection: jest.fn(),
          },
        },
      };

      instrumentation.manuallyInstrument(mockClient);

      expect(isPatched()).toBe(true);
      expect(mockClient.QdrantClient.prototype.search).toBeDefined();
      expect(mockClient.QdrantClient.prototype.upsert).toBeDefined();
    });

    it("should not double-patch", () => {
      const mockModule = {
        QdrantClient: {
          prototype: {
            search: jest.fn(),
            upsert: jest.fn(),
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);
      const firstSearch = mockModule.QdrantClient.prototype.search;

      instrumentation.manuallyInstrument(mockModule);
      const secondSearch = mockModule.QdrantClient.prototype.search;

      expect(firstSearch).toBe(secondSearch);
      expect(isPatched()).toBe(true);
    });
  });

  describe("QdrantClient.search Operation", () => {
    it("should create span with correct attributes for search operation", async () => {
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue([
          { id: 1, score: 0.95, payload: { text: "result 1" } },
          { id: 2, score: 0.89, payload: { text: "result 2" } },
        ]),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            search: mockClient.search,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        vector: new Array(768).fill(0).map(() => Math.random()),
        limit: 10,
        filter: {
          must: [{ key: "category", match: { value: "documents" } }],
        },
        with_payload: true,
        with_vector: false,
        score_threshold: 0.5,
      };

      await mockModule.QdrantClient.prototype.search.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("QdrantClient.upsert Operation", () => {
    it("should create span for upsert operation", async () => {
      const mockClient = {
        upsert: (jest.fn() as MockFn).mockResolvedValue({ status: "completed" }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            upsert: mockClient.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        points: [
          { id: 1, vector: new Array(768).fill(0.1), payload: { text: "doc1" } },
          { id: 2, vector: new Array(768).fill(0.2), payload: { text: "doc2" } },
          { id: 3, vector: new Array(768).fill(0.3), payload: { text: "doc3" } },
        ],
      };

      await mockModule.QdrantClient.prototype.upsert.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("QdrantClient.delete Operation", () => {
    it("should create span for delete by points operation", async () => {
      const mockClient = {
        delete: (jest.fn() as MockFn).mockResolvedValue({ status: "completed" }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            delete: mockClient.delete,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        points: [1, 2, 3],
      };

      await mockModule.QdrantClient.prototype.delete.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should create span for delete by filter operation", async () => {
      const mockClient = {
        delete: (jest.fn() as MockFn).mockResolvedValue({ status: "completed" }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            delete: mockClient.delete,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        filter: {
          must: [{ key: "status", match: { value: "obsolete" } }],
        },
      };

      await mockModule.QdrantClient.prototype.delete.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("QdrantClient.retrieve Operation", () => {
    it("should create span for retrieve operation", async () => {
      const mockClient = {
        retrieve: (jest.fn() as MockFn).mockResolvedValue([
          { id: 1, payload: { text: "doc1" }, vector: [0.1, 0.2] },
          { id: 2, payload: { text: "doc2" }, vector: [0.3, 0.4] },
        ]),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            retrieve: mockClient.retrieve,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        ids: [1, 2],
        with_payload: true,
        with_vector: true,
      };

      await mockModule.QdrantClient.prototype.retrieve.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("QdrantClient.scroll Operation", () => {
    it("should create span for scroll operation", async () => {
      const mockClient = {
        scroll: (jest.fn() as MockFn).mockResolvedValue({
          points: [
            { id: 1, payload: { text: "doc1" } },
            { id: 2, payload: { text: "doc2" } },
          ],
          next_page_offset: 2,
        }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            scroll: mockClient.scroll,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        limit: 100,
        with_payload: true,
        filter: { must: [{ key: "active", match: { value: true } }] },
      };

      await mockModule.QdrantClient.prototype.scroll.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("QdrantClient.count Operation", () => {
    it("should create span for count operation", async () => {
      const mockClient = {
        count: (jest.fn() as MockFn).mockResolvedValue({ count: 1500 }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            count: mockClient.count,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        filter: { must: [{ key: "category", match: { value: "active" } }] },
        exact: true,
      };

      await mockModule.QdrantClient.prototype.count.call(
        mockClient,
        "test_collection",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("QdrantClient.getCollection Operation", () => {
    it("should create span for getCollection operation", async () => {
      const mockClient = {
        getCollection: (jest.fn() as MockFn).mockResolvedValue({
          vectors_count: 10000,
          points_count: 10000,
          status: "green",
          config: {
            params: {
              vectors: { size: 768, distance: "Cosine" },
            },
          },
        }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            getCollection: mockClient.getCollection,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await mockModule.QdrantClient.prototype.getCollection.call(
        mockClient,
        "test_collection"
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on search error", async () => {
      const error = new Error("Search failed: collection not found");
      const mockClient = {
        search: (jest.fn() as MockFn).mockRejectedValue(error),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            search: mockClient.search,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.QdrantClient.prototype.search.call(
          mockClient,
          "nonexistent_collection",
          { vector: [0.1] }
        )
      ).rejects.toThrow("Search failed: collection not found");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.setStatus).toHaveBeenCalledWith({
        code: 2,
        message: "Search failed: collection not found",
      });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should record exception on upsert error", async () => {
      const error = new Error("Upsert failed: dimension mismatch");
      const mockClient = {
        upsert: (jest.fn() as MockFn).mockRejectedValue(error),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            upsert: mockClient.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.QdrantClient.prototype.upsert.call(
          mockClient,
          "test_collection",
          { points: [{ id: 1, vector: [0.1] }] }
        )
      ).rejects.toThrow("Upsert failed: dimension mismatch");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});

describe("Qdrant Instrumentation - Real World Scenarios", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();

    const { QdrantInstrumentation } = require("../instrumentation");

    instrumentation = new QdrantInstrumentation({
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

  describe("Self-Hosted RAG Pipeline", () => {
    it("should trace document ingestion workflow", async () => {
      const mockClient = {
        upsert: (jest.fn() as MockFn).mockResolvedValue({ status: "completed" }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            upsert: mockClient.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Simulate document ingestion
      const documents = [
        { id: 1, text: "Introduction to machine learning", embedding: new Array(768).fill(0.1) },
        { id: 2, text: "Deep learning fundamentals", embedding: new Array(768).fill(0.2) },
        { id: 3, text: "Natural language processing basics", embedding: new Array(768).fill(0.3) },
      ];

      const params = {
        points: documents.map(doc => ({
          id: doc.id,
          vector: doc.embedding,
          payload: {
            text: doc.text,
            source: "textbook",
            indexed_at: new Date().toISOString(),
          },
        })),
      };

      await mockModule.QdrantClient.prototype.upsert.call(
        mockClient,
        "knowledge_base",
        params
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace semantic search with filters", async () => {
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue([
          { id: 1, score: 0.95, payload: { text: "Relevant result 1" } },
          { id: 3, score: 0.88, payload: { text: "Relevant result 2" } },
        ]),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            search: mockClient.search,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // RAG retrieval with metadata filtering
      const searchParams = {
        vector: new Array(768).fill(0).map(() => Math.random()),
        limit: 5,
        with_payload: true,
        filter: {
          must: [
            { key: "source", match: { value: "textbook" } },
          ],
        },
        score_threshold: 0.7,
      };

      const results = await mockModule.QdrantClient.prototype.search.call(
        mockClient,
        "knowledge_base",
        searchParams
      );

      expect(results).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("E-commerce Product Search", () => {
    it("should trace product similarity search", async () => {
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue([
          {
            id: "prod_456",
            score: 0.93,
            payload: {
              name: "Premium Wireless Headphones",
              category: "electronics",
              price: 149.99,
              in_stock: true,
            },
          },
        ]),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            search: mockClient.search,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Find similar products with complex filters
      const searchParams = {
        vector: new Array(512).fill(0).map(() => Math.random()),
        limit: 20,
        with_payload: true,
        filter: {
          must: [
            { key: "in_stock", match: { value: true } },
            { key: "category", match: { value: "electronics" } },
          ],
          should: [
            { key: "price", range: { lte: 200 } },
          ],
        },
      };

      await mockModule.QdrantClient.prototype.search.call(
        mockClient,
        "products",
        searchParams
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace product catalog pagination", async () => {
      const mockClient = {
        scroll: (jest.fn() as MockFn).mockResolvedValue({
          points: Array.from({ length: 100 }, (_, i) => ({
            id: `prod_${i}`,
            payload: { name: `Product ${i}`, category: "electronics" },
          })),
          next_page_offset: "prod_100",
        }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            scroll: mockClient.scroll,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Paginate through products
      const scrollParams = {
        limit: 100,
        with_payload: true,
        filter: {
          must: [{ key: "category", match: { value: "electronics" } }],
        },
      };

      const result = await mockModule.QdrantClient.prototype.scroll.call(
        mockClient,
        "products",
        scrollParams
      );

      expect(result.points).toHaveLength(100);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Image Similarity Search", () => {
    it("should trace image embedding search", async () => {
      const mockClient = {
        search: (jest.fn() as MockFn).mockResolvedValue([
          { id: "img_001", score: 0.97, payload: { url: "https://example.com/1.jpg", tags: ["nature", "sunset"] } },
          { id: "img_002", score: 0.92, payload: { url: "https://example.com/2.jpg", tags: ["nature", "mountain"] } },
        ]),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            search: mockClient.search,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // CLIP-based image search
      const imageEmbedding = new Array(512).fill(0).map(() => Math.random());

      const searchParams = {
        vector: imageEmbedding,
        limit: 10,
        with_payload: true,
        filter: {
          must: [
            { key: "tags", match: { any: ["nature"] } },
          ],
        },
      };

      await mockModule.QdrantClient.prototype.search.call(
        mockClient,
        "images",
        searchParams
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection Management", () => {
    it("should trace collection statistics retrieval", async () => {
      const mockClient = {
        getCollection: (jest.fn() as MockFn).mockResolvedValue({
          vectors_count: 1000000,
          points_count: 1000000,
          indexed_vectors_count: 1000000,
          status: "green",
          optimizer_status: "ok",
          config: {
            params: {
              vectors: { size: 768, distance: "Cosine" },
              shard_number: 4,
              replication_factor: 2,
            },
          },
        }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            getCollection: mockClient.getCollection,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const collectionInfo = await mockModule.QdrantClient.prototype.getCollection.call(
        mockClient,
        "production_collection"
      );

      expect(collectionInfo.vectors_count).toBe(1000000);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace filtered count operations", async () => {
      const mockClient = {
        count: (jest.fn() as MockFn).mockResolvedValue({ count: 45000 }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            count: mockClient.count,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Count active documents
      const countParams = {
        filter: {
          must: [
            { key: "status", match: { value: "active" } },
            { key: "created_at", range: { gte: "2024-01-01" } },
          ],
        },
        exact: true,
      };

      const result = await mockModule.QdrantClient.prototype.count.call(
        mockClient,
        "documents",
        countParams
      );

      expect(result.count).toBe(45000);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Batch Operations", () => {
    it("should trace large batch upsert", async () => {
      const mockClient = {
        upsert: (jest.fn() as MockFn).mockResolvedValue({ status: "completed" }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            upsert: mockClient.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Large batch upsert
      const batchSize = 1000;
      const points = Array.from({ length: batchSize }, (_, i) => ({
        id: i,
        vector: new Array(768).fill(0).map(() => Math.random()),
        payload: {
          text: `Document ${i}`,
          batch_id: "batch_001",
          indexed_at: new Date().toISOString(),
        },
      }));

      await mockModule.QdrantClient.prototype.upsert.call(
        mockClient,
        "documents",
        { points }
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace batch deletion", async () => {
      const mockClient = {
        delete: (jest.fn() as MockFn).mockResolvedValue({ status: "completed" }),
      };

      const mockModule = {
        QdrantClient: {
          prototype: {
            delete: mockClient.delete,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Delete by filter
      const deleteParams = {
        filter: {
          must: [
            { key: "status", match: { value: "deleted" } },
          ],
        },
      };

      await mockModule.QdrantClient.prototype.delete.call(
        mockClient,
        "documents",
        deleteParams
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
