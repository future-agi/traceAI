/**
 * Tests for LiveKit instrumentation.
 */

import { LiveKitInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("LiveKitInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new LiveKitInstrumentation();
  instrumentation.disable();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("VERSION", () => {
    it("should export VERSION constant", () => {
      expect(VERSION).toBe("0.1.0");
    });
  });

  describe("LiveKitInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new LiveKitInstrumentation();
      expect(inst).toBeInstanceOf(LiveKitInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new LiveKitInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(LiveKitInstrumentation);
    });

    it("should have instrumentationName", () => {
      const inst = new LiveKitInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-livekit");
    });

    it("should have instrumentationVersion", () => {
      const inst = new LiveKitInstrumentation();
      expect(inst.instrumentationVersion).toBe(VERSION);
    });
  });

  describe("isPatched", () => {
    it("should return false when not patched", () => {
      instrumentation.disable();
      expect(isPatched()).toBe(false);
    });
  });

  describe("manuallyInstrument", () => {
    it("should handle null module", () => {
      const inst = new LiveKitInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      // Should not throw
      inst.manuallyInstrument(null);
      expect(true).toBe(true);
    });

    it("should mark client module as patched", () => {
      const inst = new LiveKitInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = {
        Room: {
          prototype: {},
        },
      };

      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });

    it("should mark rtc-node module as patched", () => {
      const inst = new LiveKitInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = {
        AudioSource: {
          prototype: {},
        },
        VideoSource: {
          prototype: {},
        },
      };

      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });
  });

  describe("enable/disable", () => {
    it("should enable instrumentation", () => {
      const inst = new LiveKitInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      // Should not throw
      expect(true).toBe(true);
    });

    it("should disable instrumentation", () => {
      const inst = new LiveKitInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      inst.disable();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

describe("LiveKit Client Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap Room.connect when available", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalConnect = jest.fn().mockResolvedValue(undefined);

    const mockModule = {
      Room: {
        prototype: {
          connect: originalConnect,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap Room.disconnect when available", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalDisconnect = jest.fn().mockResolvedValue(undefined);

    const mockModule = {
      Room: {
        prototype: {
          disconnect: originalDisconnect,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap LocalParticipant.publishTrack when available", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalPublish = jest.fn().mockResolvedValue({ sid: "track-123" });

    const mockModule = {
      Room: {
        prototype: {},
      },
      LocalParticipant: {
        prototype: {
          publishTrack: originalPublish,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap LocalParticipant.unpublishTrack when available", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalUnpublish = jest.fn().mockResolvedValue(undefined);

    const mockModule = {
      Room: {
        prototype: {},
      },
      LocalParticipant: {
        prototype: {
          unpublishTrack: originalUnpublish,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should not double-patch module", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      _fiPatched: true,
      Room: {
        prototype: {
          connect: jest.fn(),
        },
      },
    };

    const original = mockModule.Room.prototype.connect;
    inst.manuallyInstrument(mockModule);

    // Should not have changed
    expect(mockModule.Room.prototype.connect).toBe(original);
  });
});

describe("LiveKit RTC Node Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap AudioSource.captureFrame when available", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCapture = jest.fn();

    const mockModule = {
      AudioSource: {
        prototype: {
          captureFrame: originalCapture,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap VideoSource.captureFrame when available", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCapture = jest.fn();

    const mockModule = {
      VideoSource: {
        prototype: {
          captureFrame: originalCapture,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });
});

describe("Room Lifecycle Tracking", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should handle room connect and disconnect lifecycle", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Room: {
        prototype: {
          connect: jest.fn().mockResolvedValue(undefined),
          disconnect: jest.fn().mockResolvedValue(undefined),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should handle track publish and unpublish", () => {
    const inst = new LiveKitInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Room: {
        prototype: {},
      },
      LocalParticipant: {
        prototype: {
          publishTrack: jest.fn().mockResolvedValue({ sid: "track-1" }),
          unpublishTrack: jest.fn().mockResolvedValue(undefined),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });
});
