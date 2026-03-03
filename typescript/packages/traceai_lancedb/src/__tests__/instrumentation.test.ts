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

let LanceDBInstrumentation: any;
let isPatched: any;

describe("LanceDB Instrumentation", () => {
  let instrumentation: any;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
    const mod = require("../instrumentation");
    LanceDBInstrumentation = mod.LanceDBInstrumentation;
    isPatched = mod.isPatched;
    instrumentation = new LanceDBInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation?.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(LanceDBInstrumentation);
    });

    it("should initialize with default config", () => {
      const inst = new LanceDBInstrumentation();
      expect(inst).toBeInstanceOf(LanceDBInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/lancedb");
    });
  });

  describe("Patching", () => {
    it("should report not patched initially", () => {
      expect(isPatched()).toBe(false);
    });

    it("should patch Table methods", () => {
      const mockModule = {
        Table: {
          prototype: {
            search: jest.fn(),
            add: jest.fn(),
          },
        },
        Connection: {
          prototype: {
            createTable: jest.fn(),
          },
        },
      };
      instrumentation.manuallyInstrument(mockModule);
      expect(isPatched()).toBe(true);
    });
  });

  describe("Search Operation", () => {
    it("should create span for search operation", async () => {
      const mockTable = {
        name: "test_table",
        search: (jest.fn() as MockFn).mockResolvedValue([]),
      };
      const mockModule = {
        Table: { prototype: { search: mockTable.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await mockModule.Table.prototype.search.call(mockTable, { limit: 10 });

      expect(mockSpan.setStatus).toHaveBeenCalledWith({ code: 1 });
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });

  describe("Error Handling", () => {
    it("should record exception on error", async () => {
      const error = new Error("Search failed");
      const mockTable = {
        search: (jest.fn() as MockFn).mockRejectedValue(error),
      };
      const mockModule = {
        Table: { prototype: { search: mockTable.search } },
      };
      instrumentation.manuallyInstrument(mockModule);

      await expect(
        mockModule.Table.prototype.search.call(mockTable, {})
      ).rejects.toThrow("Search failed");

      expect(mockSpan.recordException).toHaveBeenCalledWith(error);
      expect(mockSpan.end).toHaveBeenCalled();
    });
  });
});
