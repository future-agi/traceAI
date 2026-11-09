import { parse as parseProto } from 'protobufjs';
import { isSensitiveKey, redact, type ResolvedConfig } from '@traceai/agent-core';

// OTLP wire field numbers are stable. Unknown fields are deliberately discarded at this boundary.
const schema = `syntax = "proto3";
message AnyValue { oneof value { string string_value=1; bool bool_value=2; int64 int_value=3; double double_value=4; ArrayValue array_value=5; KeyValueList kvlist_value=6; bytes bytes_value=7; } }
message ArrayValue { repeated AnyValue values=1; }
message KeyValueList { repeated KeyValue values=1; }
message KeyValue { string key=1; AnyValue value=2; }
message Resource { repeated KeyValue attributes=1; uint32 dropped_attributes_count=2; }
message Scope { string name=1; string version=2; repeated KeyValue attributes=3; uint32 dropped_attributes_count=4; }
message Event { fixed64 time_unix_nano=1; string name=2; repeated KeyValue attributes=3; uint32 dropped_attributes_count=4; }
message Link { bytes trace_id=1; bytes span_id=2; string trace_state=3; repeated KeyValue attributes=4; uint32 dropped_attributes_count=5; fixed32 flags=6; }
message Status { string message=2; int32 code=3; }
message Span { bytes trace_id=1; bytes span_id=2; string trace_state=3; bytes parent_span_id=4; string name=5; int32 kind=6; fixed64 start_time_unix_nano=7; fixed64 end_time_unix_nano=8; repeated KeyValue attributes=9; uint32 dropped_attributes_count=10; repeated Event events=11; uint32 dropped_events_count=12; repeated Link links=13; uint32 dropped_links_count=14; Status status=15; fixed32 flags=16; }
message ScopeSpans { Scope scope=1; repeated Span spans=2; string schema_url=3; }
message ResourceSpans { Resource resource=1; repeated ScopeSpans scope_spans=2; string schema_url=3; }
message ExportTraceServiceRequest { repeated ResourceSpans resource_spans=1; }`;
export const TraceRequest = parseProto(schema).root.lookupType('ExportTraceServiceRequest');
type Obj = Record<string, any>;
const structural = /^(?:gen_ai\.(?:operation\.name|system|provider\.name|request\.model|response\.(?:model|id)|usage\.[a-z_.]+|cost\.[a-z_.]+|server\.[a-z_.]+)|llm\.(?:model_name|provider|token_count\.[a-z_]+)|tool\.name|service\.(?:name|version)|telemetry\.sdk\.(?:name|version|language)|gen_ai\.(?:tool\.name|tool\.call\.id|agent\.name|conversation\.id)|fi\.(?:span\.kind|agent\.(?:name|surface|outcome|version))|error\.type|session\.id)$/;
// Exact semantic-convention counters, not arbitrary names containing "token".
// Only numeric OTLP values qualify; strings/containers cannot use this exemption.
const numericTokenFields = new Set([
  'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'gen_ai.usage.total_tokens',
  'gen_ai.usage.cache_read_tokens', 'gen_ai.usage.cache_write_tokens',
  'gen_ai.usage.cache_creation_tokens', 'gen_ai.usage.reasoning_tokens',
  'gen_ai.usage.input_tokens.audio', 'gen_ai.usage.input_tokens.cache_read',
  'gen_ai.usage.input_tokens.cache_write', 'gen_ai.usage.output_tokens.audio',
  'gen_ai.usage.output_tokens.reasoning', 'gen_ai.usage.cache_read.input_tokens',
  'gen_ai.usage.cache_write.input_tokens', 'gen_ai.usage.reasoning.output_tokens',
  'llm.token_count.prompt', 'llm.token_count.completion', 'llm.token_count.total',
  'llm.token_count.prompt_details.cache_read', 'llm.token_count.prompt_details.cache_write',
  'llm.token_count.prompt_details.audio', 'llm.token_count.completion_details.reasoning',
  'llm.token_count.completion_details.audio', 'gen_ai.server.time_to_first_token',
]);
function safeAttributeKey(x: Obj): boolean {
  if (!x || typeof x.key !== 'string' || x.key.length > 256) return false;
  if (!isSensitiveKey(x.key)) return true;
  const value = x.value;
  if (!numericTokenFields.has(x.key) || !value || Object.keys(value).length !== 1) return false;
  return typeof value.intValue === 'string' && /^\d{1,20}$/.test(value.intValue) ||
    typeof value.intValue === 'number' && Number.isSafeInteger(value.intValue) && value.intValue >= 0 ||
    typeof value.doubleValue === 'number' && Number.isFinite(value.doubleValue) && value.doubleValue >= 0;
}
function safeValue(v: Obj, depth = 0): Obj {
  if (!v || typeof v !== 'object' || depth > 12) return { stringValue: '[OMITTED]' };
  if (typeof v.stringValue === 'string') return { stringValue: redact(v.stringValue).slice(0, 32768) };
  if (typeof v.boolValue === 'boolean') return { boolValue: v.boolValue };
  if (typeof v.intValue === 'string' && /^-?\d{1,20}$/.test(v.intValue)) return { intValue: v.intValue };
  if (typeof v.intValue === 'number' && Number.isSafeInteger(v.intValue)) return { intValue: v.intValue };
  if (typeof v.doubleValue === 'number' && Number.isFinite(v.doubleValue)) return { doubleValue: v.doubleValue };
  if (Array.isArray(v.arrayValue?.values)) return { arrayValue: { values: v.arrayValue.values.slice(0, 128).map((x: Obj) => safeValue(x, depth + 1)) } };
  if (Array.isArray(v.kvlistValue?.values)) return { kvlistValue: { values: v.kvlistValue.values.slice(0, 128)
    .filter(safeAttributeKey)
    .map((x: Obj) => ({ key: x.key, value: safeValue(x.value, depth + 1) })) } };
  // Opaque bytes cannot be redacted safely; omit them instead of forwarding hidden credentials.
  return { stringValue: '[OMITTED]' };
}
function attrs(list: unknown, restricted: boolean): Obj[] {
  if (!Array.isArray(list)) return [];
  return list.slice(0, 256).filter(x => safeAttributeKey(x) && (!restricted || structural.test(x.key)))
    .map(x => ({ key: x.key, value: safeValue(x.value) }));
}
const pick = (o: Obj, keys: string[]) => Object.fromEntries(keys.filter(k => o[k] !== undefined).map(k => [k, o[k]]));
/** Conservative privacy ceiling: any hide switch strips all potentially content-bearing attributes/events.
 * Native IDs and per-call token accounting pass through; no synthetic request spans are added.
 */
export function sanitize(input: Obj, config: Pick<ResolvedConfig, 'captureContent'|'hideInputs'|'hideOutputs'|'hideErrorDetails'>, projectDir?: string) {
  if (!input || !Array.isArray(input.resourceSpans)) throw new Error('INVALID_OTLP');
  const restricted = !config.captureContent || config.hideInputs || config.hideOutputs || config.hideErrorDetails;
  let count = 0;
  const resourceSpans = input.resourceSpans.map((r: Obj) => {
    if (!r || !Array.isArray(r.scopeSpans)) throw new Error('INVALID_OTLP');
    // Application-level settings receive all windows. A project-scoped route MUST carry
    // native workspace attribution; never guess the project from whichever window bound first.
    if (projectDir) {
      const roots = (r.resource?.attributes ?? []).filter((a: Obj) => ['process.cwd', 'vcs.repository.root'].includes(a.key));
      if (!roots.length || roots.some((a: Obj) => a.value?.stringValue !== projectDir)) throw new Error('WORKSPACE_ATTRIBUTION_REQUIRED');
    }
    return {
      resource: { attributes: attrs(r.resource?.attributes, restricted) },
      scopeSpans: r.scopeSpans.map((s: Obj) => {
        if (!s || !Array.isArray(s.spans)) throw new Error('INVALID_OTLP');
        return {
          scope: { name: redact(String(s.scope?.name ?? '')).slice(0, 256), version: String(s.scope?.version ?? '').slice(0, 64), attributes: attrs(s.scope?.attributes, restricted) },
          spans: s.spans.map((span: Obj) => {
            if (!span || ++count > 512) throw new Error('SPAN_LIMIT');
            const name = typeof span.name === 'string' ? span.name : '';
            const attributes = attrs(span.attributes, restricted);
            const operation = attributes.find(a => a.key === 'gen_ai.operation.name')?.value?.stringValue;
            const out: Obj = {
              ...pick(span, ['traceId','spanId','parentSpanId','kind','startTimeUnixNano','endTimeUnixNano','flags']),
              name: restricted ? (['invoke_agent','chat','execute_tool','embedding'].includes(operation) ? operation : 'activity') : redact(name).slice(0, 256),
              attributes,
              status: { code: [0,1,2].includes(span.status?.code) ? span.status.code :
                span.status?.code === 'STATUS_CODE_ERROR' ? 2 : span.status?.code === 'STATUS_CODE_OK' ? 1 : 0 },
              events: restricted ? [] : (Array.isArray(span.events) ? span.events.slice(0, 64).map((e: Obj) => ({
                timeUnixNano: e.timeUnixNano, name: redact(String(e.name ?? '')).slice(0, 256), attributes: attrs(e.attributes, false)
              })) : []),
              links: Array.isArray(span.links) ? span.links.slice(0, 32).map((l: Obj) => ({
                ...pick(l, ['traceId','spanId','flags']), attributes: attrs(l.attributes, restricted)
              })) : []
            };
            if (!restricted && span.status?.message) out.status.message = redact(String(span.status.message)).slice(0, 32768);
            return out;
          })
        };
      })
    };
  });
  return { body: { resourceSpans }, count };
}
export function decode(body: Buffer, protobuf: boolean): Obj {
  if (!protobuf) return JSON.parse(body.toString('utf8'));
  return TraceRequest.toObject(TraceRequest.decode(body), { longs: String, bytes: Buffer });
}
export function encode(body: Obj, protobuf: boolean): Buffer {
  return protobuf ? Buffer.from(TraceRequest.encode(TraceRequest.fromObject(body)).finish()) : Buffer.from(JSON.stringify(body));
}
