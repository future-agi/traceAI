import { createServer } from 'node:http';
import protobuf from 'protobufjs';
import { createFlueContext } from '@flue/runtime/internal';

// Test-only OTLP wire schema: decode the actual protobuf request, not an SDK
// span object. Field numbers are the OpenTelemetry protocol's stable contract.
const Request = protobuf.parse(`syntax = "proto3";
message Value { string stringValue = 1; bool boolValue = 2; int64 intValue = 3; double doubleValue = 4; ArrayValue arrayValue = 5; }
message ArrayValue { repeated Value values = 1; }
message KV { string key = 1; Value value = 2; }
message Resource { repeated KV attributes = 1; }
message Status { string message = 2; int32 code = 3; }
message Event { string name = 2; repeated KV attributes = 3; }
message Span { bytes traceId = 1; bytes spanId = 2; bytes parentSpanId = 4; string name = 5; repeated KV attributes = 9; repeated Event events = 11; Status status = 15; }
message ScopeSpans { repeated Span spans = 2; }
message ResourceSpans { Resource resource = 1; repeated ScopeSpans scopeSpans = 2; }
message Request { repeated ResourceSpans resourceSpans = 1; }
`).root.lookupType('Request');
const value = (v) => v.stringValue ?? v.boolValue ?? (v.intValue === undefined ? undefined : Number(v.intValue)) ?? v.doubleValue ?? v.arrayValue?.values?.map(value);
export const attrs = (items = []) => Object.fromEntries(items.map(({ key, value: v }) => [key, value(v)]));

export async function recorder({ hang = false, delay = 0 } = {}) {
  const requests = [];
  const server = createServer(async (req, res) => {
    const parts = [];
    for await (const part of req) parts.push(part);
    const body = Buffer.concat(parts);
    const decoded = Request.toObject(Request.decode(body), { longs: String, bytes: String });
    const spans = (decoded.resourceSpans ?? []).flatMap((rs) => (rs.scopeSpans ?? []).flatMap((ss) => (ss.spans ?? []).map((span) => ({
      ...span, attributes: attrs(span.attributes), resource: attrs(rs.resource?.attributes),
      events: span.events?.map((event) => ({ ...event, attributes: attrs(event.attributes) })),
    }))));
    requests.push({ url: req.url, headers: req.headers, body: body.toString(), spans });
    if (!hang) setTimeout(() => res.writeHead(200, { 'content-type': 'application/x-protobuf' }).end(), delay);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    requests, url: `http://127.0.0.1:${server.address().port}`,
    close: async () => { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); },
  };
}

// Only the test uses Flue's internal event fixture API. The implementation uses
// public instrument() and the official OTel adapter without private hooks.
export function events(suffix = '') {
  const ctx = createFlueContext({ id: `instance${suffix}`, agentName: 'Weather', env: {}, agentConfig: {} });
  const common = { operationId: `operation${suffix}`, conversationId: `conversation${suffix}`, operationKind: 'prompt', agentName: 'Weather' };
  const emit = (event) => ctx.emitEvent({ ...common, ...event });
  return {
    emit,
    start: () => emit({ type: 'operation_start' }),
    end: () => emit({ type: 'operation', isError: false, durationMs: 1, agentInput: 'PRIVATE_PROMPT', agentOutput: 'PRIVATE_OUTPUT' }),
    tool: () => {
      emit({ type: 'tool_start', toolName: 'weather', toolCallId: 'tool1', args: { secret: 'PRIVATE_TOOL' } });
      emit({ type: 'tool', toolName: 'weather', toolCallId: 'tool1', durationMs: 1, isError: true, errorInfo: { type: 'tool_error', name: 'Error', message: 'PRIVATE_ERROR', stack: 'PRIVATE_STACK' } });
    },
  };
}

export async function waitFor(predicate, timeout = 5000) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeout) throw new Error('Timed out waiting for test condition');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
