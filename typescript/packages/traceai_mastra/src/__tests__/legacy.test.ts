import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { FITraceExporter } from "../FITraceExporter";
import { ATTR_PROJECT_NAME, SemanticConventions } from "@traceai/fi-semantic-conventions";
import { isFISpan } from "../utils";

describe("legacy conversion data-only boundary", () => {
  it("enriches a real native SDK span without touching shared data or executing value hooks", async () => {
    const hook = jest.fn(() => { throw new Error("application hook ran"); });
    const date = new Date("2020-01-01T00:00:00Z");
    Object.defineProperty(date, "toJSON", { value: hook });
    const proxy = new Proxy({}, { get: hook, getPrototypeOf: hook, getOwnPropertyDescriptor: hook, ownKeys: hook });
    const resource = resourceFromAttributes({ "service.name": "legacy-service" });
    const provider = new BasicTracerProvider({ resource });
    const native = provider.getTracer("legacy-app").startSpan("agent legacy") as any;
    native.attributes["app.date"] = date;
    native.attributes["app.proxy"] = proxy;
    Object.defineProperty(native.attributes, "app.getter", { enumerable: true, get: hook });
    native.end();
    const exported: any[] = [];
    const serialize = jest.spyOn(OTLPTraceExporter.prototype, "export").mockImplementation((spans, callback) => {
      exported.push(...spans); callback({ code: 0 });
    });
    const exporter = new FITraceExporter({ url: "http://127.0.0.1:1/traces" });
    try {
      const callback = jest.fn();
      exporter.export([native], callback);
      expect(callback).toHaveBeenCalledWith({ code: 0 });
      expect(exported).toHaveLength(1);
      expect(exported[0].spanContext()).toEqual(native.spanContext());
      expect(exported[0].attributes[SemanticConventions.FI_SPAN_KIND]).toBe("AGENT");
      expect(exported[0].attributes["app.date"]).toBe("2020-01-01T00:00:00.000Z");
      expect(exported[0].attributes["app.proxy"]).toBe("object");
      expect(exported[0].resource.attributes[ATTR_PROJECT_NAME]).toBe("legacy-service");
      expect(resource.attributes[ATTR_PROJECT_NAME]).toBeUndefined();
      expect(native.attributes["app.date"]).toBe(date);
      expect(native.attributes[SemanticConventions.FI_SPAN_KIND]).toBeUndefined();
      expect(hook).not.toHaveBeenCalled();
    } finally { serialize.mockRestore(); await exporter.shutdown(); await provider.shutdown(); }
  });

  it("preserves native merged-resource precedence and application project routing", async () => {
    const resource = resourceFromAttributes({ "service.name": "default-service", "app.attribute": "default", "app.null": "fallback" })
      .merge(resourceFromAttributes({ "service.name": "application-service", "app.attribute": "application", "app.null": undefined }));
    const provider = new BasicTracerProvider({ resource });
    const native = provider.getTracer("legacy-app").startSpan("agent legacy") as any;
    native.end();
    const exported: any[] = [];
    const serialize = jest.spyOn(OTLPTraceExporter.prototype, "export").mockImplementation((spans, callback) => {
      exported.push(...spans); callback({ code: 0 });
    });
    const exporter = new FITraceExporter({ url: "http://127.0.0.1:1/traces" });
    try {
      exporter.export([native], jest.fn());
      expect(exported).toHaveLength(1);
      expect(exported[0].resource.attributes).toEqual({
        ...resource.attributes, [ATTR_PROJECT_NAME]: "application-service",
      });
      expect(resource.attributes[ATTR_PROJECT_NAME]).toBeUndefined();
    } finally { serialize.mockRestore(); await exporter.shutdown(); await provider.shutdown(); }
  });

  it("does not call unsupported spanContext getters, functions or Proxy traps", async () => {
    const hook = jest.fn(() => { throw new Error("application hook ran"); });
    const proxy = new Proxy({}, { get: hook, getPrototypeOf: hook, getOwnPropertyDescriptor: hook, ownKeys: hook });
    const serialize = jest.spyOn(OTLPTraceExporter.prototype, "export").mockImplementation((spans, callback) => {
      expect(spans).toHaveLength(0); callback({ code: 0 });
    });
    const exporter = new FITraceExporter({ url: "http://127.0.0.1:1/traces" });
    try {
      exporter.export([proxy, { spanContext: hook }, Object.defineProperty({}, "attributes", { get: hook })] as any, jest.fn());
      expect(isFISpan(proxy as any)).toBe(false);
      expect(isFISpan(Object.defineProperty({}, "attributes", { get: hook }) as any)).toBe(false);
      expect(hook).not.toHaveBeenCalled();
    } finally { serialize.mockRestore(); await exporter.shutdown(); }
  });
});
