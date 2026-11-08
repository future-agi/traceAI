import type { Attributes, Context, Span } from '@opentelemetry/api';
import type { ReadableSpan, SpanProcessor } from '@opentelemetry/sdk-trace-base';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { ProjectType } from '@traceai/fi-core';
import { SemanticConventions as SC } from '@traceai/fi-semantic-conventions';
import { BoundedSpanProcessor, collectionEnabled, redact, type ResolvedConfig } from '@traceai/agent-core';

const spanNames = new Set([
  'openclaw.harness.run', 'openclaw.run', 'openclaw.context.assembled',
  'openclaw.model.call', 'openclaw.tool.execution', 'openclaw.exec',
  'openclaw.context.compaction', 'openclaw.agent.compaction', 'openclaw.skill.used',
  'openclaw.diagnostic.phase',
]);
const contentFields = {
  'openclaw.content.input_messages': 'input.value',
  'openclaw.content.output_messages': 'output.value',
  'openclaw.content.tool_input': 'input.value',
  'openclaw.content.tool_output': 'output.value',
} as const;
const labels = new Set([
  'openclaw.provider', 'openclaw.model', 'openclaw.api', 'openclaw.agent',
  'openclaw.toolName', 'openclaw.tool.source', 'openclaw.outcome',
  'openclaw.errorCategory', 'openclaw.failureKind',
  'openclaw.model_call.observation_unit', 'gen_ai.operation.name',
  'gen_ai.provider.name', 'gen_ai.system', 'gen_ai.request.model', 'gen_ai.tool.name', 'gen_ai.tool.call.id',
]);
const numeric = new Set([
  'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens',
  'gen_ai.usage.cache_read.input_tokens', 'gen_ai.usage.cache_creation.input_tokens',
  'openclaw.model_call.usage.reasoning_output_tokens',
  'openclaw.model_call.time_to_first_byte_ms',
]);
function text(value: string, config: ResolvedConfig): string {
  // The core bounds oversized input and inspects complete structured/encoded
  // values. Truncating first could hide a credential's key or closing delimiter.
  let clean = redact(value);
  for (const secret of [config.credentials?.apiKey, config.credentials?.secretKey]) {
    if (secret) clean = clean.split(secret).join('[REDACTED]');
  }
  return clean.slice(0, 20000);
}

function failedToolResult(span: ReadableSpan): boolean {
  if (span.status.code === 2) return true;
  const output = span.attributes['openclaw.content.tool_output'];
  if (span.name !== 'openclaw.tool.execution' || typeof output !== 'string' || output.length > 256 * 1024) return false;
  try {
    const value = JSON.parse(output);
    return !!value && typeof value === 'object' && !Array.isArray(value) &&
      (value.isError === true || value.is_error === true || value.success === false ||
        value.type === 'error' || value.status === 'error' || value.status === 'failed');
  } catch { return false; }
}

function isModelSpan(span: ReadableSpan): boolean {
  if (span.name === 'openclaw.model.call') return true;
  const operation = span.attributes['gen_ai.operation.name'];
  const unit = span.attributes['openclaw.model_call.observation_unit'];
  // Pinned host's experimental names, not arbitrary GenAI spans or usage rollups.
  if (unit === 'turn') return operation === 'invoke_agent' && span.name === 'invoke_agent';
  return unit === 'request' && typeof operation === 'string' &&
    ['chat', 'text_completion', 'generate_content'].includes(operation) &&
    span.name.startsWith(operation + ' ') &&
    /^[a-zA-Z0-9_.:/-]{1,128}$/.test(span.name.slice(operation.length + 1)) && !span.name.includes('://');
}
/** Pure native transformation: the capture side retains native coordinates.
 * The launcher subsequently attaches these trees to its invocation-owned prompt
 * context and records the native coordinates as provenance before export.
 * The native model.usage span repeats successful model.call usage. It is always suppressed,
 * not counted again or redistributed to retries. Unknown native span families fail closed.
 */
export function normalizeSpan(span: ReadableSpan, config: ResolvedConfig, session?: string): ReadableSpan | undefined {
  const unit = span.attributes['openclaw.model_call.observation_unit'];
  const validModel = isModelSpan(span);
  const nativeModelFamily = unit !== undefined && ['chat', 'text_completion', 'generate_content', 'invoke_agent']
    .some(operation => span.name === operation || span.name.startsWith(operation + ' '));
  const model = validModel || nativeModelFamily;
  if (!spanNames.has(span.name) && !model) return;
  const tool = span.name === 'openclaw.tool.execution';
  const failed = failedToolResult(span);
  const unknownShape = model && (!validModel || (unit !== undefined && unit !== 'request' && unit !== 'turn'));
  const generation = model && !unknownShape && unit !== 'turn';
  const ownsUsage = model && !unknownShape;
  const attributes: Attributes = {
    'fi.span.kind': generation ? 'LLM' : tool ? 'TOOL' : 'AGENT',
    'fi.agent.name': 'openclaw',
    'fi.agent.trace_unit': 'native-run',
    'fi.agent.turn_correlation': 'unverified',
    'fi.agent.usage_owner': generation ? 'model-request' : ownsUsage ? 'native-turn' : 'none',
    [SC.FI_AGENT_TIMING]: 'source',
    [SC.FI_AGENT_FIDELITY]: generation ? 'native-request' : ownsUsage ? 'aggregate-turn' : 'unknown',
    [SC.FI_USAGE_CONTRIBUTION]: ownsUsage ? 'additive' : 'none',
    [SC.FI_AGENT_USAGE_ADDITIVE]: ownsUsage,
    ...(ownsUsage ? { [SC.FI_USAGE_SCOPE]: generation ? 'request' : 'turn',
      [SC.FI_AGENT_USAGE_PLAN]: generation ? 'request' : 'turn' } : {}),
    ...(unknownShape ? { 'fi.agent.gap': 'UNSUPPORTED_NATIVE_SPAN_SHAPE' } : {}),
  };
  if (session) attributes['session.id'] = session;
  if (config.userId) attributes['user.id'] = config.userId;
  for (const [key, value] of Object.entries(span.attributes)) {
    if (labels.has(key) && typeof value === 'string' && /^[a-zA-Z0-9_.:/-]{1,128}$/.test(value) && !value.includes('://'))
      attributes[key] = text(value, config);
    if (ownsUsage && numeric.has(key) && typeof value === 'number' && Number.isFinite(value) && value >= 0)
      attributes[key] = value;
    const target = contentFields[key as keyof typeof contentFields];
    // Native input_messages is serialized conversation history, not just the
    // current prompt. It can contain assistant output and past tool errors even
    // when this model request succeeds. Omit this mixed field under either
    // privacy switch rather than trusting roles/error markers in an opaque or
    // truncated host payload. Independently captured tool inputs remain visible.
    const hiddenHistory = key === 'openclaw.content.input_messages' &&
      (config.hideOutputs || config.hideErrorDetails);
    if (!unknownShape && !hiddenHistory && target && typeof value === 'string' && !(target === 'input.value'
      ? config.hideInputs
      : config.hideOutputs || (config.hideErrorDetails && failed)))
      attributes[target] = text(value, config);
  }
  if (tool) {
    const name = attributes['gen_ai.tool.name'] ?? attributes['openclaw.toolName'];
    if (typeof name === 'string') attributes[SC.TOOL_NAME] = name;
    const callId = attributes['gen_ai.tool.call.id'];
    if (typeof callId === 'string') attributes[SC.TOOL_CALL_ID] = callId;
  }
  if (ownsUsage) {
    for (const [native, canonical] of [
      ['gen_ai.usage.cache_read.input_tokens', SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ],
      ['gen_ai.usage.cache_creation.input_tokens', SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE],
    ]) {
      if (typeof attributes[native] === 'number') attributes[canonical] = attributes[native];
      delete attributes[native];
    }
    const input = attributes['gen_ai.usage.input_tokens'], output = attributes['gen_ai.usage.output_tokens'];
    if (typeof input === 'number' && typeof output === 'number') attributes['gen_ai.usage.total_tokens'] = input + output;
    if (attributes['gen_ai.provider.name'] === undefined && attributes['gen_ai.system'] !== undefined)
      attributes['gen_ai.provider.name'] = attributes['gen_ai.system'];
    const reasoning = attributes['openclaw.model_call.usage.reasoning_output_tokens'];
    if (typeof reasoning === 'number') attributes['gen_ai.usage.output_tokens.reasoning'] = reasoning;
    const ttft = attributes['openclaw.model_call.time_to_first_byte_ms'];
    if (typeof ttft === 'number') attributes['fi.agent.time_to_first_byte_ms'] = ttft;
  }
  // Rebuild resource/events/status rather than forwarding host names, argv, paths, tool
  // definitions, arbitrary diagnostic payloads, or hidden content through a second field.
  return {
    ...span,
    // Experimental names include a host-controlled model label; export the stable family name.
    name: model ? 'openclaw.model.call' : span.name,
    // SDK ReadableSpan fields can be prototype getters, not enumerable own
    // properties. Preserve native timing/parentage explicitly across the bridge.
    spanContext: () => span.spanContext(),
    parentSpanContext: span.parentSpanContext,
    kind: span.kind, startTime: span.startTime, endTime: span.endTime,
    duration: span.duration, ended: span.ended,
    instrumentationScope: span.instrumentationScope,
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
    attributes,
    resource: resourceFromAttributes({
      'service.name': 'openclaw', project_name: config.projectName, project_type: ProjectType.OBSERVE,
    }),
    events: [], links: [],
    status: { code: failed ? 2 : span.status.code, ...(unknownShape || config.hideErrorDetails || !span.status.message ? {} : { message: text(span.status.message, config) }) },
  };
}

export class NativeSpanProcessor implements SpanProcessor {
  readonly delivery: BoundedSpanProcessor;
  constructor(private readonly config: ResolvedConfig, private readonly session?: string) {
    this.delivery = new BoundedSpanProcessor(config);
  }
  onStart(_span: Span, _context: Context): void {}
  onEnd(span: ReadableSpan): void {
    try {
      if (!collectionEnabled(this.config)) return;
      const normalized = normalizeSpan(span, this.config, this.session);
      if (normalized) this.delivery.onEnd(normalized);
    } catch { /* Telemetry must not affect a model or tool. */ }
  }
  forceFlush(): Promise<void> { return this.delivery.forceFlush(); }
  drain(): Promise<void> { return this.delivery.drain(); }
  shutdown(): Promise<void> { return this.delivery.shutdown(); }
}
