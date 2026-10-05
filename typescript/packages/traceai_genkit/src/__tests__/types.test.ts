/**
 * Type-level contract with the pinned genkit: FIGenkitSpanProcessor must be
 * accepted by `enableTelemetry({ spanProcessors })`, whose config type is
 * `TelemetryConfig = Partial<NodeSDKConfiguration>` (@genkit-ai/core
 * src/telemetryTypes.ts:24 at 1.42.0). ts-jest type-checks this file, so a
 * type error fails the suite.
 */
import type { TelemetryConfig } from "genkit";
import type { enableTelemetry, flushTracing } from "genkit/tracing";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { FIGenkitSpanProcessor } from "../index";

type EnableTelemetryArg = Parameters<typeof enableTelemetry>[0];

describe("genkit 1.42.0 type compatibility", () => {
  it("is a valid spanProcessors entry of TelemetryConfig and enableTelemetry's argument", () => {
    const processor = new FIGenkitSpanProcessor({ tracerProvider: new BasicTracerProvider() });
    const config: TelemetryConfig = { spanProcessors: [processor] };
    const arg: EnableTelemetryArg = config;
    expect(arg.spanProcessors).toHaveLength(1);
  });

  it("flushTracing is the exported flush function and returns a promise", () => {
    type Flush = typeof flushTracing;
    const check: ReturnType<Flush> extends Promise<void> ? true : false = true;
    expect(check).toBe(true);
  });

  it("TelemetryConfig has no forceDevExport / disableMetrics (those are @genkit-ai/google-cloud options)", () => {
    // @ts-expect-error disableMetrics is not a TelemetryConfig key at genkit 1.42.0
    const a: TelemetryConfig = { disableMetrics: true };
    // @ts-expect-error forceDevExport is not a TelemetryConfig key at genkit 1.42.0
    const b: TelemetryConfig = { forceDevExport: true };
    expect([a, b]).toHaveLength(2);
  });
});
