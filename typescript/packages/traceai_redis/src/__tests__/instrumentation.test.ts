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

let RedisInstrumentation: any;
let isPatched: any;

describe("Redis Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
    const mod = require("../instrumentation");
    RedisInstrumentation = mod.RedisInstrumentation;
    isPatched = mod.isPatched;
    instrumentation = new RedisInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation?.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(RedisInstrumentation);
    });

    it("should initialize with default config", () => {
      const inst = new RedisInstrumentation();
      expect(inst).toBeInstanceOf(RedisInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/redis");
    });

    it("should accept custom configuration", () => {
      const inst = new RedisInstrumentation({
        instrumentationConfig: { captureQueryVectors: true },
        traceConfig: { maskInputs: true },
      });
      expect(inst).toBeInstanceOf(RedisInstrumentation);
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch createClient function", () => {
      const mockClient = {
        ft: { search: jest.fn(), create: jest.fn() },
        hSet: jest.fn(),
        hGet: jest.fn(),
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      expect(isPatched()).toBe(true);

      const client = mockModule.createClient() as typeof mockClient;
      expect(client.ft.search).toBeDefined();
    });
  });

  describe("FT.SEARCH Operation", () => {
    it("should create span for ftSearch operation", async () => {
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 2,
        documents: [{ id: "1" }, { id: "2" }],
      });
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await client.ft.search("myindex", "*", { LIMIT: { from: 0, size: 10 } });

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle KNN vector search", async () => {
      const queryVector = Buffer.from(new Float32Array([0.1, 0.2, 0.3]).buffer);
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 5,
        documents: [
          { id: "doc:1", value: { name: "Product 1", score: "0.95" } },
          { id: "doc:2", value: { name: "Product 2", score: "0.87" } },
        ],
      });
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      const result = await client.ft.search(
        "idx:products",
        "*=>[KNN 10 @embedding $vector AS score]",
        {
          PARAMS: { vector: queryVector },
          RETURN: ["name", "price", "score"],
          SORTBY: { BY: "score" },
          DIALECT: 2,
        }
      );

      expect(result.total).toBe(5);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle filtered vector search", async () => {
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 3,
        documents: [],
      });
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await client.ft.search(
        "idx:products",
        "(@category:{electronics})=>[KNN 20 @embedding $vector AS score]",
        {
          PARAMS: { vector: Buffer.from(new Float32Array([0.1, 0.2]).buffer) },
          DIALECT: 2,
        }
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("FT.CREATE Operation", () => {
    it("should trace index creation", async () => {
      const mockFtCreate = (jest.fn() as MockFn).mockResolvedValue("OK");
      const mockClient = {
        ft: { create: mockFtCreate },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await client.ft.create("idx:products", {
        "$.name": { type: "TEXT", AS: "name" },
        "$.embedding": {
          type: "VECTOR",
          AS: "embedding",
          ALGORITHM: "HNSW",
          TYPE: "FLOAT32",
          DIM: 384,
          DISTANCE_METRIC: "COSINE",
        },
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Hash Operations", () => {
    it("should trace HSET operation", async () => {
      const mockHSet = (jest.fn() as MockFn).mockResolvedValue(1);
      const mockClient = {
        hSet: mockHSet,
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await client.hSet("product:1", {
        name: "Laptop",
        price: "999",
        embedding: Buffer.from(new Float32Array([0.1, 0.2]).buffer).toString("base64"),
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace HGET operation", async () => {
      const mockHGet = (jest.fn() as MockFn).mockResolvedValue("Laptop");
      const mockClient = {
        hGet: mockHGet,
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      const result = await client.hGet("product:1", "name");

      expect(result).toBe("Laptop");
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("JSON Operations", () => {
    it("should trace JSON.SET operation", async () => {
      const mockJsonSet = (jest.fn() as MockFn).mockResolvedValue("OK");
      const mockClient = {
        json: { set: mockJsonSet },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await client.json.set("product:1", "$", {
        name: "Laptop Pro",
        price: 1299,
        embedding: [0.1, 0.2, 0.3],
      });

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace JSON.GET operation", async () => {
      const mockJsonGet = (jest.fn() as MockFn).mockResolvedValue({
        name: "Laptop Pro",
        price: 1299,
      });
      const mockClient = {
        json: { get: mockJsonGet },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      const result = await client.json.get("product:1", { path: ["$.name", "$.price"] });

      expect(result.name).toBe("Laptop Pro");
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on error", async () => {
      const error = new Error("Search failed");
      const mockFtSearch = (jest.fn() as MockFn).mockRejectedValue(error);
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await expect(client.ft.search("myindex", "*")).rejects.toThrow("Search failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle connection errors", async () => {
      const connectionError = new Error("Connection refused");
      const mockFtSearch = (jest.fn() as MockFn).mockRejectedValue(connectionError);
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await expect(client.ft.search("idx", "*")).rejects.toThrow("Connection refused");

      expect(mockSpan.recordException).toHaveBeenCalled();
    });

    it("should handle index not found error", async () => {
      const notFoundError = new Error("Unknown index name");
      const mockFtSearch = (jest.fn() as MockFn).mockRejectedValue(notFoundError);
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      await expect(client.ft.search("missing_index", "*")).rejects.toThrow("Unknown index");

      expect(mockSpan.recordException).toHaveBeenCalled();
    });
  });

  describe("Real-World Scenarios", () => {
    it("should handle e-commerce product search", async () => {
      const queryVector = Buffer.from(new Float32Array(384).fill(0.1).buffer);
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 50,
        documents: [
          { id: "product:1", value: { name: "Laptop", price: "999", score: "0.95" } },
          { id: "product:2", value: { name: "Tablet", price: "499", score: "0.87" } },
        ],
      });
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      const result = await client.ft.search(
        "idx:products",
        "(@category:{electronics} @price:[100 1000])=>[KNN 20 @embedding $vector AS score]",
        {
          PARAMS: { vector: queryVector },
          RETURN: ["name", "price", "category", "score"],
          SORTBY: { BY: "score" },
          LIMIT: { from: 0, size: 20 },
          DIALECT: 2,
        }
      );

      expect(result.total).toBe(50);
      expect(result.documents).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should handle real-time recommendation caching", async () => {
      const mockJsonSet = (jest.fn() as MockFn).mockResolvedValue("OK");
      const mockExpire = (jest.fn() as MockFn).mockResolvedValue(1);
      const mockClient = {
        json: { set: mockJsonSet },
        expire: mockExpire,
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      // Cache recommendations
      const recommendations = [
        { itemId: "1", name: "Product 1", score: 0.95 },
        { itemId: "2", name: "Product 2", score: 0.87 },
      ];

      for (const rec of recommendations) {
        await client.json.set(`rec:user123:${rec.itemId}`, "$", {
          ...rec,
          embedding: new Array(384).fill(0),
          cachedAt: Date.now(),
        });
        await client.expire(`rec:user123:${rec.itemId}`, 3600);
      }

      expect(mockSpan.end).toHaveBeenCalledTimes(2); // 2 json.set operations
    });

    it("should handle session-based semantic search", async () => {
      const mockJsonGet = (jest.fn() as MockFn).mockResolvedValue({
        queries: [
          { text: "laptop", embedding: new Array(384).fill(0.1), timestamp: Date.now() - 60000 },
        ],
      });
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 10,
        documents: [{ id: "doc:1", value: { title: "Best Laptops 2024" } }],
      });
      const mockJsonArrAppend = (jest.fn() as MockFn).mockResolvedValue(2);
      const mockClient = {
        json: { get: mockJsonGet, arrAppend: mockJsonArrAppend },
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      // Get session context
      const sessionData = await client.json.get("session:abc123");
      expect(sessionData.queries).toHaveLength(1);

      // Perform search
      const searchResult = await client.ft.search(
        "idx:content",
        "*=>[KNN 10 @embedding $vector AS score]",
        { PARAMS: { vector: Buffer.from(new Float32Array(384).fill(0.1).buffer) }, DIALECT: 2 }
      );
      expect(searchResult.total).toBe(10);

      // Update session (note: arrAppend may not be instrumented)
      await client.json.arrAppend("session:abc123", "$.queries", {
        text: "gaming laptop",
        embedding: new Array(384).fill(0.2),
        timestamp: Date.now(),
      });

      expect(mockSpan.end).toHaveBeenCalledTimes(2); // json.get + ft.search
    });

    it("should handle anomaly detection workflow", async () => {
      const metricVector = Buffer.from(new Float32Array(64).fill(0.5).buffer);
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 10,
        documents: [
          { id: "metric:1", value: { label: "normal", similarity: "0.92" } },
          { id: "metric:2", value: { label: "normal", similarity: "0.88" } },
          { id: "metric:3", value: { label: "anomaly", similarity: "0.75" } },
        ],
      });
      const mockJsonSet = (jest.fn() as MockFn).mockResolvedValue("OK");
      const mockClient = {
        ft: { search: mockFtSearch },
        json: { set: mockJsonSet },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      // Search for similar patterns
      const result = await client.ft.search(
        "idx:metrics",
        "*=>[KNN 10 @embedding $vector AS similarity]",
        {
          PARAMS: { vector: metricVector },
          RETURN: ["timestamp", "label", "similarity"],
          DIALECT: 2,
        }
      );

      const normalPatterns = result.documents.filter(
        (doc: any) => doc.value.label === "normal" && parseFloat(doc.value.similarity) > 0.8
      );

      const isAnomaly = normalPatterns.length < 3;
      expect(isAnomaly).toBe(true);

      // Store anomaly
      if (isAnomaly) {
        await client.json.set(`anomaly:${Date.now()}`, "$", {
          embedding: Array.from(new Float32Array(64).fill(0.5)),
          timestamp: Date.now(),
          similarPatterns: result.documents.slice(0, 3),
        });
      }

      expect(mockSpan.end).toHaveBeenCalledTimes(2);
    });

    it("should handle multi-index federated search", async () => {
      const queryVector = Buffer.from(new Float32Array(384).fill(0.1).buffer);
      const mockFtSearch = (jest.fn() as MockFn)
        .mockResolvedValueOnce({
          total: 5,
          documents: [{ id: "product:1", value: { name: "Laptop" } }],
        })
        .mockResolvedValueOnce({
          total: 3,
          documents: [{ id: "article:1", value: { title: "Tech Review" } }],
        })
        .mockResolvedValueOnce({
          total: 2,
          documents: [{ id: "faq:1", value: { question: "How to..." } }],
        });
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      // Search across multiple indices
      const [products, articles, faqs] = await Promise.all([
        client.ft.search("idx:products", "*=>[KNN 5 @embedding $vector]", {
          PARAMS: { vector: queryVector },
          DIALECT: 2,
        }),
        client.ft.search("idx:articles", "*=>[KNN 5 @embedding $vector]", {
          PARAMS: { vector: queryVector },
          DIALECT: 2,
        }),
        client.ft.search("idx:faqs", "*=>[KNN 5 @embedding $vector]", {
          PARAMS: { vector: queryVector },
          DIALECT: 2,
        }),
      ]);

      expect(products.total).toBe(5);
      expect(articles.total).toBe(3);
      expect(faqs.total).toBe(2);
      expect(mockSpan.end).toHaveBeenCalledTimes(3);
    });

    it("should handle geo + vector hybrid search", async () => {
      const queryVector = Buffer.from(new Float32Array(384).fill(0.1).buffer);
      const mockFtSearch = (jest.fn() as MockFn).mockResolvedValue({
        total: 15,
        documents: [
          { id: "place:1", value: { name: "Coffee Shop", address: "123 Main St", relevance: "0.92" } },
          { id: "place:2", value: { name: "Cafe", address: "456 Oak Ave", relevance: "0.85" } },
        ],
      });
      const mockClient = {
        ft: { search: mockFtSearch },
      };
      const mockModule = {
        createClient: jest.fn().mockReturnValue(mockClient),
      };

      instrumentation.manuallyInstrument(mockModule);
      const client = mockModule.createClient() as typeof mockClient;

      const result = await client.ft.search(
        "idx:places",
        "@location:[-122.4194 37.7749 5 km]=>[KNN 20 @embedding $vector AS relevance]",
        {
          PARAMS: { vector: queryVector },
          RETURN: ["name", "address", "location", "relevance"],
          SORTBY: { BY: "relevance" },
          DIALECT: 2,
        }
      );

      expect(result.total).toBe(15);
      expect(result.documents).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
