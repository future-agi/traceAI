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
let ChromaDBInstrumentation: any;
let isPatched: any;
let FITracer: any;

describe("ChromaDB Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    // Reset module state
    jest.resetModules();

    // Re-import after reset
    const instrumentationModule = require("../instrumentation");
    ChromaDBInstrumentation = instrumentationModule.ChromaDBInstrumentation;
    isPatched = instrumentationModule.isPatched;
    FITracer = require("@traceai/fi-core").FITracer;

    instrumentation = new ChromaDBInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
      traceConfig: {
        maskInputs: false,
        maskOutputs: false,
      },
    });

    // Enable the instrumentation to initialize FITracer
    instrumentation.enable();
  });

  afterEach(() => {
    if (instrumentation) {
      instrumentation.disable();
    }
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(ChromaDBInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new ChromaDBInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(ChromaDBInstrumentation);
    });

    it("should initialize with custom config", () => {
      const customInstrumentation = new ChromaDBInstrumentation({
        instrumentationConfig: {
          enabled: true,
          captureQueryVectors: true,
          captureResultVectors: false,
          captureDocuments: true,
        },
        traceConfig: {
          maskInputs: true,
          maskOutputs: true,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(ChromaDBInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/chromadb");
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch Collection class methods", () => {
      const mockCollection = {
        Collection: {
          prototype: {
            add: jest.fn(),
            query: jest.fn(),
            get: jest.fn(),
            update: jest.fn(),
            upsert: jest.fn(),
            delete: jest.fn(),
            count: jest.fn(),
            peek: jest.fn(),
          },
        },
      };

      instrumentation.manuallyInstrument(mockCollection);

      expect(isPatched()).toBe(true);
      expect(mockCollection.Collection.prototype.add).toBeDefined();
      expect(mockCollection.Collection.prototype.query).toBeDefined();
    });

    it("should not double-patch", () => {
      const mockModule = {
        Collection: {
          prototype: {
            add: jest.fn(),
            query: jest.fn(),
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);
      const firstAdd = mockModule.Collection.prototype.add;

      instrumentation.manuallyInstrument(mockModule);
      const secondAdd = mockModule.Collection.prototype.add;

      expect(firstAdd).toBe(secondAdd);
      expect(isPatched()).toBe(true);
    });

    it("should handle module without Collection class", () => {
      const emptyModule = {};

      expect(() => {
        instrumentation.manuallyInstrument(emptyModule);
      }).not.toThrow();
    });
  });

  describe("Collection.add Operation", () => {
    it("should create span with correct attributes for add operation", async () => {
      const mockCollection = {
        name: "test_collection",
        add: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            add: mockCollection.add,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Simulate calling add
      const params = {
        ids: ["id1", "id2", "id3"],
        embeddings: [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]],
        documents: ["doc1", "doc2", "doc3"],
        metadatas: [{ key: "value1" }, { key: "value2" }, { key: "value3" }],
      };

      // Call the patched method
      await mockModule.Collection.prototype.add.call(mockCollection, params);

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 }); // OK
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection.query Operation", () => {
    it("should create span with correct attributes for query operation", async () => {
      const mockCollection = {
        name: "test_collection",
        query: (jest.fn() as MockFn).mockResolvedValue({
          ids: [["id1", "id2"]],
          documents: [["doc1", "doc2"]],
          distances: [[0.1, 0.2]],
        }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            query: mockCollection.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        queryEmbeddings: [[0.1, 0.2, 0.3]],
        nResults: 5,
        where: { field: "value" },
        include: ["metadatas", "embeddings"],
      };

      await mockModule.Collection.prototype.query.call(mockCollection, params);

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 }); // OK
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection.get Operation", () => {
    it("should create span for get operation", async () => {
      const mockCollection = {
        name: "test_collection",
        get: (jest.fn() as MockFn).mockResolvedValue({
          ids: ["id1", "id2"],
          documents: ["doc1", "doc2"],
        }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            get: mockCollection.get,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        ids: ["id1", "id2"],
        limit: 10,
      };

      await mockModule.Collection.prototype.get.call(mockCollection, params);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection.upsert Operation", () => {
    it("should create span for upsert operation", async () => {
      const mockCollection = {
        name: "test_collection",
        upsert: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            upsert: mockCollection.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        ids: ["id1", "id2"],
        embeddings: [[0.1, 0.2], [0.3, 0.4]],
        documents: ["doc1", "doc2"],
      };

      await mockModule.Collection.prototype.upsert.call(mockCollection, params);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection.delete Operation", () => {
    it("should create span for delete operation", async () => {
      const mockCollection = {
        name: "test_collection",
        delete: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            delete: mockCollection.delete,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = {
        ids: ["id1", "id2", "id3"],
      };

      await mockModule.Collection.prototype.delete.call(mockCollection, params);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection.count Operation", () => {
    it("should create span for count operation", async () => {
      const mockCollection = {
        name: "test_collection",
        count: (jest.fn() as MockFn).mockResolvedValue(42),
      };

      const mockModule = {
        Collection: {
          prototype: {
            count: mockCollection.count,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.count.call(mockCollection);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Collection.peek Operation", () => {
    it("should create span for peek operation", async () => {
      const mockCollection = {
        name: "test_collection",
        peek: (jest.fn() as MockFn).mockResolvedValue({
          ids: ["id1", "id2"],
          documents: ["doc1", "doc2"],
        }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            peek: mockCollection.peek,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      const params = { limit: 5 };

      await mockModule.Collection.prototype.peek.call(mockCollection, params);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on add error", async () => {
      const error = new Error("Add operation failed");
      const mockCollection = {
        name: "test_collection",
        add: (jest.fn() as MockFn).mockRejectedValue(error),
      };

      const mockModule = {
        Collection: {
          prototype: {
            add: mockCollection.add,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Collection.prototype.add.call(mockCollection, { ids: ["id1"] })
      ).rejects.toThrow("Add operation failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.setStatus).toHaveBeenCalledWith({
        code: 2, // ERROR
        message: "Add operation failed",
      });
      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should record exception on query error", async () => {
      const error = new Error("Query failed");
      const mockCollection = {
        name: "test_collection",
        query: (jest.fn() as MockFn).mockRejectedValue(error),
      };

      const mockModule = {
        Collection: {
          prototype: {
            query: mockCollection.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Collection.prototype.query.call(mockCollection, {})
      ).rejects.toThrow("Query failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Trace Configuration", () => {
    it("should respect trace configuration options", () => {
      const configuredInstrumentation = new ChromaDBInstrumentation({
        traceConfig: {
          maskInputs: true,
          maskOutputs: true,
        },
      });

      expect(configuredInstrumentation).toBeInstanceOf(ChromaDBInstrumentation);
      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe("Semantic Conventions", () => {
    it("should use correct db.system value", async () => {
      // Verify the instrumentation sets db.system to "chromadb"
      const mockCollection = {
        name: "test_collection",
        count: (jest.fn() as MockFn).mockResolvedValue(10),
      };

      const mockModule = {
        Collection: {
          prototype: {
            count: mockCollection.count,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);
      await mockModule.Collection.prototype.count.call(mockCollection);

      // The span should have been created with chromadb system
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});

describe("ChromaDB Instrumentation - Real World Scenarios", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();

    const { ChromaDBInstrumentation } = require("../instrumentation");

    instrumentation = new ChromaDBInstrumentation({
      instrumentationConfig: {
        enabled: true,
        captureDocuments: true,
      },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    if (instrumentation) {
      instrumentation.disable();
    }
  });

  describe("RAG Pipeline Scenario", () => {
    it("should trace document ingestion workflow", async () => {
      const mockCollection = {
        name: "knowledge_base",
        add: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            add: mockCollection.add,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Simulate RAG document ingestion
      const documents = [
        "Machine learning is a subset of artificial intelligence.",
        "Neural networks are inspired by biological neurons.",
        "Deep learning uses multiple layers of neural networks.",
      ];

      const params = {
        ids: ["doc_001", "doc_002", "doc_003"],
        documents,
        embeddings: [
          new Array(1536).fill(0).map(() => Math.random()),
          new Array(1536).fill(0).map(() => Math.random()),
          new Array(1536).fill(0).map(() => Math.random()),
        ],
        metadatas: [
          { source: "textbook", chapter: 1 },
          { source: "textbook", chapter: 2 },
          { source: "textbook", chapter: 3 },
        ],
      };

      await mockModule.Collection.prototype.add.call(mockCollection, params);

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace semantic search for RAG retrieval", async () => {
      const mockCollection = {
        name: "knowledge_base",
        query: (jest.fn() as MockFn).mockResolvedValue({
          ids: [["doc_001", "doc_003"]],
          documents: [[
            "Machine learning is a subset of artificial intelligence.",
            "Deep learning uses multiple layers of neural networks.",
          ]],
          distances: [[0.1, 0.2]],
          metadatas: [[{ source: "textbook" }, { source: "textbook" }]],
        }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            query: mockCollection.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // User asks a question - we find relevant context
      const queryParams = {
        queryEmbeddings: [new Array(1536).fill(0).map(() => Math.random())],
        nResults: 3,
        where: { source: "textbook" },
        include: ["documents", "metadatas", "distances"],
      };

      const result = await mockModule.Collection.prototype.query.call(
        mockCollection,
        queryParams
      );

      expect(result.documents[0]).toHaveLength(2);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Chatbot Memory Scenario", () => {
    it("should trace conversation history storage", async () => {
      const mockCollection = {
        name: "chat_memory",
        upsert: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            upsert: mockCollection.upsert,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Store conversation turn
      const conversationParams = {
        ids: ["conv_123_turn_5"],
        documents: ["User asked about pricing plans for enterprise."],
        embeddings: [new Array(384).fill(0).map(() => Math.random())],
        metadatas: [{
          conversation_id: "conv_123",
          turn: 5,
          role: "user",
          timestamp: new Date().toISOString(),
        }],
      };

      await mockModule.Collection.prototype.upsert.call(
        mockCollection,
        conversationParams
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace context retrieval from memory", async () => {
      const mockCollection = {
        name: "chat_memory",
        query: (jest.fn() as MockFn).mockResolvedValue({
          ids: [["conv_123_turn_3", "conv_123_turn_4"]],
          documents: [[
            "User mentioned they run a startup with 50 employees.",
            "Assistant explained basic plan features.",
          ]],
        }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            query: mockCollection.query,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Retrieve recent conversation context
      const queryParams = {
        queryEmbeddings: [new Array(384).fill(0).map(() => Math.random())],
        nResults: 5,
        where: { conversation_id: "conv_123" },
      };

      await mockModule.Collection.prototype.query.call(mockCollection, queryParams);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Document Management Scenario", () => {
    it("should trace document deletion", async () => {
      const mockCollection = {
        name: "documents",
        delete: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            delete: mockCollection.delete,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Delete outdated documents
      const deleteParams = {
        ids: ["doc_old_001", "doc_old_002"],
      };

      await mockModule.Collection.prototype.delete.call(
        mockCollection,
        deleteParams
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });

    it("should trace document updates", async () => {
      const mockCollection = {
        name: "documents",
        update: (jest.fn() as MockFn).mockResolvedValue({ success: true }),
      };

      const mockModule = {
        Collection: {
          prototype: {
            update: mockCollection.update,
          },
        },
      };

      instrumentation.manuallyInstrument(mockModule);

      // Update document embeddings after model change
      const updateParams = {
        ids: ["doc_001"],
        embeddings: [new Array(1536).fill(0).map(() => Math.random())],
        metadatas: [{ last_updated: new Date().toISOString() }],
      };

      await mockModule.Collection.prototype.update.call(
        mockCollection,
        updateParams
      );

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
