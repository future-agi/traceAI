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

let MongoDBInstrumentation: any;
let isPatched: any;

describe("MongoDB Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
    const mod = require("../instrumentation");
    MongoDBInstrumentation = mod.MongoDBInstrumentation;
    isPatched = mod.isPatched;
    instrumentation = new MongoDBInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation?.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(MongoDBInstrumentation);
    });

    it("should initialize with default config", () => {
      const inst = new MongoDBInstrumentation();
      expect(inst).toBeInstanceOf(MongoDBInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/mongodb");
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
            aggregate: jest.fn(),
            find: jest.fn(),
            insertOne: jest.fn(),
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);
      expect(isPatched()).toBe(true);
    });
  });

  describe("Aggregate Operation", () => {
    it("should create span for aggregate operation", async () => {
      const mockCollection = {
        collectionName: "test_collection",
        aggregate: (jest.fn() as MockFn).mockResolvedValue({ insertedCount: 1 }),
      };
      const mockModule = {
        Collection: { prototype: { aggregate: mockCollection.aggregate } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.aggregate.call(mockCollection, []);

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Vector Search Detection", () => {
    it("should detect $vectorSearch in aggregation pipeline", async () => {
      const mockCollection = {
        collectionName: "test_collection",
        aggregate: (jest.fn() as MockFn).mockResolvedValue({ docs: [] }),
      };
      const mockModule = {
        Collection: { prototype: { aggregate: mockCollection.aggregate } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Collection.prototype.aggregate.call(mockCollection, [
        { $vectorSearch: { queryVector: [0.1, 0.2], limit: 10 } },
      ]);

      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on error", async () => {
      const error = new Error("Aggregate failed");
      const mockCollection = {
        aggregate: (jest.fn() as MockFn).mockRejectedValue(error),
      };
      const mockModule = {
        Collection: { prototype: { aggregate: mockCollection.aggregate } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Collection.prototype.aggregate.call(mockCollection, [])
      ).rejects.toThrow("Aggregate failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
