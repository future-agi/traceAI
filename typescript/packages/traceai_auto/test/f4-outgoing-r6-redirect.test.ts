import { ClientRequest } from "node:http";
import { context, ROOT_CONTEXT, trace, propagation } from "@opentelemetry/api";
import { installOutgoingPropagation } from "../src/outgoing-node";
import { compileOutgoingPolicy } from "../src/outgoing-policy";
import { createOutgoingPrivacy, noteOutgoingSpanStart } from "../src/outgoing-privacy";
import { AlwaysOnSampler, BasicTracerProvider } from "@opentelemetry/sdk-trace-base";

const policy = compileOutgoingPolicy(undefined, undefined), privacy = createOutgoingPrivacy();
// R-LIT: synthetic W3C identity; fresh objects isolate each test (F4-R6-context).
function fresh() {
  return propagation.setBaggage(trace.setSpanContext(ROOT_CONTEXT, { traceId: "13579bdf13579bdf13579bdf13579bdf", spanId: "2468ace02468ace0", traceFlags: 0, isRemote: true }), propagation.createBaggage({ region: { value: "west" } }));
}
const prepare = (ctx: ReturnType<typeof fresh>) => privacy.prepareOutgoingHeaders(ctx, new URL("http://orders.internal"), [], policy);

test.each([301, 302, 303, 307, 308])("R6 response fence precedes listeners and closes unprovable %s origins", statusCode => {
  const ctx = fresh(), unrelated = fresh(), active = jest.spyOn(context, "active").mockReturnValue(ctx);
  const remove = installOutgoingPropagation(policy, privacy, [], jest.fn(), true);
  const req = Object.create(ClientRequest.prototype);
  const order: string[] = [];
  req.on("response", () => { order.push("one"); expect(prepare(ctx)).toBeUndefined(); });
  req.on("response", () => { order.push("two"); expect(prepare(ctx)).toBeUndefined(); });
  try {
    expect(prepare(ctx)?.has("traceparent")).toBe(true);
    expect(req.emit("response", { statusCode, rawHeaders: ["Location", "http://invoices.internal"] })).toBe(true);
    expect(order).toEqual(["one", "two"]);
    expect(prepare(ctx)).toBeUndefined();
    expect(prepare(unrelated)?.has("traceparent")).toBe(true);
    expect(prepare(ROOT_CONTEXT)).toBeUndefined();
  } finally { remove(); active.mockRestore(); }
});

test("R6 a child allocated before Location and a baggage-only identity stay closed", async () => {
  const ctx = fresh(), active = jest.spyOn(context, "active").mockReturnValue(ctx);
  // This positive control must record the child despite fresh()'s unsampled parent.
  // Retain the separate unsampled-remote/opaque-local no-addition controls.
  const provider = new BasicTracerProvider({ sampler: new AlwaysOnSampler(), spanProcessors: [{ onStart: noteOutgoingSpanStart, onEnd() {}, async forceFlush() {}, async shutdown() {} }] });
  const child = provider.getTracer("f4-redirect").startSpan("precreated", {}, ctx);
  const childCtx = trace.setSpan(ROOT_CONTEXT, child);
  const remove = installOutgoingPropagation(policy, privacy, [], jest.fn(), true);
  const req = Object.create(ClientRequest.prototype); req.on("response", () => {});
  try {
    expect(child.isRecording()).toBe(true);
    expect(child.spanContext().traceFlags & 1).toBe(1);
    expect(prepare(childCtx)?.has("traceparent")).toBe(true);
    req.emit("response", { statusCode: 307, rawHeaders: ["Location", "http://invoices.internal"] });
    expect(prepare(childCtx)).toBeUndefined();
    const baggageCtx = propagation.setBaggage(ROOT_CONTEXT, propagation.createBaggage({ region: { value: "west" } }));
    active.mockReturnValue(baggageCtx);
    expect(prepare(baggageCtx)?.get("baggage")).toBe("region=west");
    req.emit("response", { statusCode: 307, rawHeaders: ["Location", "http://invoices.internal"] });
    expect(prepare(baggageCtx)).toBeUndefined();
  } finally { child.end(); remove(); active.mockRestore(); await provider.shutdown(); }
});

test("R6 response listener exceptions retain identity and are never retried", () => {
  const ctx = fresh(), active = jest.spyOn(context, "active").mockReturnValue(ctx);
  const remove = installOutgoingPropagation(policy, privacy, [], jest.fn(), true);
  const req = Object.create(ClientRequest.prototype), error = new Error("synthetic response-listener failure"), listener = jest.fn(() => { throw error; });
  req.on("response", listener);
  try {
    let thrown: unknown;
    try { req.emit("response", { statusCode: 307, rawHeaders: ["Location", "http://invoices.internal"] }); } catch (caught) { thrown = caught; }
    expect(thrown).toBe(error); expect(listener).toHaveBeenCalledTimes(1);
    expect(prepare(ctx)).toBeUndefined();
  } finally { remove(); active.mockRestore(); }
});

test("R6 shutdown restores owned response hooks and leaves retained foreign hooks inert", () => {
  const proto: any = ClientRequest.prototype, before = Object.getOwnPropertyDescriptor(proto, "emit");
  const ctx = fresh(), active = jest.spyOn(context, "active").mockReturnValue(ctx);
  const remove = installOutgoingPropagation(policy, privacy, [], jest.fn(), true);
  try {
    const owned = proto.emit;
    expect(Object.getOwnPropertyDescriptor(proto, "emit")?.value).toBe(owned);
    const foreign = function(this: unknown, ...args: unknown[]) { return Reflect.apply(owned, this, args); };
    Object.defineProperty(proto, "emit", { configurable: true, writable: true, value: foreign });
    remove(); remove(); expect(proto.emit).toBe(foreign);
    const req = Object.create(proto), listener = jest.fn(); req.on("response", listener);
    req.emit("response", { statusCode: 307, rawHeaders: ["Location", "http://invoices.internal"] });
    expect(listener).toHaveBeenCalledTimes(1); expect(prepare(ctx)?.has("traceparent")).toBe(true);
  } finally {
    remove(); active.mockRestore();
    if (before) Object.defineProperty(proto, "emit", before); else delete proto.emit;
  }
  const second = installOutgoingPropagation(policy, privacy, [], jest.fn(), true); second();
  expect(Object.getOwnPropertyDescriptor(proto, "emit")).toEqual(before);
});
