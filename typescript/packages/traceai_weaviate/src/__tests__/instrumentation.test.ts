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

let WeaviateInstrumentation: any;
let isPatched: any;

describe("Weaviate Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
    const mod = require("../instrumentation");
    WeaviateInstrumentation = mod.WeaviateInstrumentation;
    isPatched = mod.isPatched;
    instrumentation = new WeaviateInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation?.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(WeaviateInstrumentation);
    });

    it("should initialize with default config", () => {
      const inst = new WeaviateInstrumentation();
      expect(inst).toBeInstanceOf(WeaviateInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/weaviate");
    });

    it("should accept custom trace config", () => {
      const inst = new WeaviateInstrumentation({
        traceConfig: { maskInputs: true, maskOutputs: true },
      });
      expect(inst).toBeInstanceOf(WeaviateInstrumentation);
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch Collection methods", () => {
      const mockModule = {
        Collection: {
          prototype: {
            query: jest.fn(),
            aggregate: jest.fn(),
          },
        },
        WeaviateClient: {
          prototype: {},
        },
      };
      instrumentation.manuallyInstrument(mockModule);
      expect(isPatched()).toBe(true);
    });

    it("should not double-patch", () => {
      const mockModule = {
        Collection: {
          prototype: { query: jest.fn() },
        },
      };
      instrumentation.manuallyInstrument(mockModule);
      const first = mockModule.Collection.prototype.query;
      instrumentation.manuallyInstrument(mockModule);
      expect(mockModule.Collection.prototype.query).toBe(first);
    });
  });

  describe("Query Operations", () => {
    it("should create span for query operation", async () => {
      const mockCollection = {
        name: "test_collection",
        query: (jest.fn() as MockFn).mockResolvedValue({ objects: [] }),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.query.call(mockCollection, { limit: 10 });

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle nearText search", async () => {
      const mockCollection = {
        name: "articles",
        query: (jest.fn() as MockFn).mockResolvedValue({
          objects: [
            { properties: { title: "ML Basics" } },
            { properties: { title: "Deep Learning" } },
          ],
        }),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Collection.prototype.query.call(mockCollection, {
        nearText: "machine learning",
        limit: 5,
      });

      expect(result.objects).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle hybrid search", async () => {
      const mockCollection = {
        name: "products",
        query: (jest.fn() as MockFn).mockResolvedValue({ objects: [] }),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.query.call(mockCollection, {
        hybrid: { query: "laptop", alpha: 0.5 },
        limit: 20,
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle nearVector search", async () => {
      const mockVector = new Array(384).fill(0).map(() => Math.random());
      const mockCollection = {
        name: "embeddings",
        query: (jest.fn() as MockFn).mockResolvedValue({ objects: [] }),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.query.call(mockCollection, {
        nearVector: mockVector,
        limit: 10,
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Data Operations", () => {
    it("should trace insert operation", async () => {
      const mockCollection = {
        name: "documents",
        data: {
          insert: (jest.fn() as MockFn).mockResolvedValue({ id: "uuid-123" }),
        },
      };
      const mockModule = {
        Collection: {
          prototype: {
            data: mockCollection.data,
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.data.insert.call(mockCollection, {
        properties: { title: "Test Document" },
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace insertMany operation", async () => {
      const mockCollection = {
        name: "documents",
        data: {
          insertMany: (jest.fn() as MockFn).mockResolvedValue({
            uuids: ["uuid-1", "uuid-2", "uuid-3"],
          }),
        },
      };
      const mockModule = {
        Collection: {
          prototype: {
            data: mockCollection.data,
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.data.insertMany.call(mockCollection, [
        { properties: { title: "Doc 1" } },
        { properties: { title: "Doc 2" } },
        { properties: { title: "Doc 3" } },
      ]);

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace delete operation", async () => {
      const mockCollection = {
        name: "documents",
        data: {
          deleteById: (jest.fn() as MockFn).mockResolvedValue(true),
        },
      };
      const mockModule = {
        Collection: {
          prototype: {
            data: mockCollection.data,
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.data.deleteById.call(
        mockCollection,
        "uuid-123"
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Aggregate Operations", () => {
    it("should trace aggregate operation", async () => {
      const mockCollection = {
        name: "products",
        aggregate: (jest.fn() as MockFn).mockResolvedValue({
          totalCount: 100,
          properties: { price: { mean: 49.99 } },
        }),
      };
      const mockModule = {
        Collection: { prototype: { aggregate: mockCollection.aggregate } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Collection.prototype.aggregate.call(
        mockCollection,
        { groupBy: "category" }
      );

      expect(result.totalCount).toBe(100);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on error", async () => {
      const error = new Error("Query failed");
      const mockCollection = {
        name: "test_collection",
        query: (jest.fn() as MockFn).mockRejectedValue(error),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Collection.prototype.query.call(mockCollection, {})
      ).rejects.toThrow("Query failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle connection errors", async () => {
      const connectionError = new Error("Connection refused");
      const mockCollection = {
        name: "test_collection",
        query: (jest.fn() as MockFn).mockRejectedValue(connectionError),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Collection.prototype.query.call(mockCollection, {})
      ).rejects.toThrow("Connection refused");

      expect(mockSpan.recordException).toHaveBeenCalledWith(connectionError);
    });

    it("should handle timeout errors", async () => {
      const timeoutError = new Error("Request timeout");
      const mockCollection = {
        name: "test_collection",
        query: (jest.fn() as MockFn).mockRejectedValue(timeoutError),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Collection.prototype.query.call(mockCollection, {})
      ).rejects.toThrow("Request timeout");

      expect(mockSpan.recordException).toHaveBeenCalled();
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Real-World Scenarios", () => {
    it("should handle RAG retrieval workflow", async () => {
      const mockCollection = {
        name: "knowledge_base",
        query: (jest.fn() as MockFn).mockResolvedValue({
          objects: [
            {
              properties: {
                content: "Machine learning is...",
                source: "textbook",
              },
              metadata: { certainty: 0.95 },
            },
            {
              properties: {
                content: "Neural networks are...",
                source: "article",
              },
              metadata: { certainty: 0.87 },
            },
          ],
        }),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Collection.prototype.query.call(
        mockCollection,
        {
          nearText: "What is machine learning?",
          limit: 5,
          returnProperties: ["content", "source"],
        }
      );

      expect(result.objects).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle multi-tenant search", async () => {
      const mockCollection = {
        name: "tenant_data",
        query: (jest.fn() as MockFn).mockResolvedValue({ objects: [] }),
      };
      const mockModule = {
        Collection: { prototype: { query: mockCollection.query } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.query.call(mockCollection, {
        nearText: "important document",
        where: {
          path: ["tenantId"],
          operator: "Equal",
          valueText: "tenant-123",
        },
        limit: 10,
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle batch document ingestion", async () => {
      const documents = Array.from({ length: 100 }, (_, i) => ({
        properties: { title: `Document ${i}`, content: `Content for doc ${i}` },
      }));

      const mockCollection = {
        name: "documents",
        data: {
          insertMany: (jest.fn() as MockFn).mockResolvedValue({
            uuids: documents.map((_, i) => `uuid-${i}`),
          }),
        },
      };
      const mockModule = {
        Collection: {
          prototype: {
            data: mockCollection.data,
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);

      const result = await mockModule.Collection.prototype.data.insertMany.call(
        mockCollection,
        documents
      );

      expect(result.uuids).toHaveLength(100);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
