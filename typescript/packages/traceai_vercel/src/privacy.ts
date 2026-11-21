import { Attributes, diag } from '@opentelemetry/api';
import { ReadableSpan } from '@opentelemetry/sdk-trace-base';
import { addFIAttributesToSpan } from './utils';

const SWITCH_ON = ['true', '1', 'yes', 'on'];
const SWITCH_OFF = ['false', '0', 'no', 'off'];
const warnedSwitches = new Set<string>();

/** Reads one privacy switch. A switch someone set must never silently do nothing: true, 1,
 * yes and on turn it on, false, 0, no and off turn it off (any case, surrounding spaces
 * ignored), and an empty value counts as unset. Any other value turns it on, with one
 * warning per variable that names the accepted values and never the value.
 */
function readSwitch(name: string): boolean | undefined {
  const normalized = process.env[name]?.trim().toLowerCase();
  if (normalized === undefined || normalized === '') return undefined;
  if (SWITCH_ON.includes(normalized)) return true;
  if (SWITCH_OFF.includes(normalized)) return false;
  if (!warnedSwitches.has(name)) {
    warnedSwitches.add(name);
    try {
      diag.warn(`@traceai/vercel: ${name} is not one of true, 1, yes, on, false, 0, no or off; treating it as on, so the content it controls is hidden.`);
    } catch { /* Diagnostics must also fail open. */ }
  }
  return true;
}

/** Snapshot the FI environment contract once, when a processor is constructed.
 * Keep this adapter independent of fi-core, which requires OpenTelemetry SDK 2.x.
 */
export function readPrivacyConfig() {
  const enabled = (name: string) => readSwitch(name) === true;
  const hideInputs = enabled('FI_HIDE_INPUTS') || enabled('FI_HIDE_INPUT_MESSAGES') || enabled('FI_HIDE_INPUT_TEXT');
  const hideOutputs = enabled('FI_HIDE_OUTPUTS') || enabled('FI_HIDE_OUTPUT_MESSAGES') || enabled('FI_HIDE_OUTPUT_TEXT');
  const hideErrorDetails = readSwitch('FI_HIDE_ERROR_DETAILS');
  return {
    hideInputs: hideInputs || enabled('FI_HIDE_INPUT_IMAGES'),
    hideOutputs,
    hideEmbeddingVectors: enabled('FI_HIDE_EMBEDDING_VECTORS'),
    // Match fi-core's tri-state: an unset value derives from content hiding.
    hideErrorDetails: hideErrorDetails ?? (hideInputs || hideOutputs),
  };
}

type PrivacyConfig = ReturnType<typeof readPrivacyConfig>;

// These are serialized payloads, often containing several content modalities.
// Drop the whole payload for any applicable content-hiding flag: selectively
// masking JSON would leave alternate native/normalized copies unprotected.
const inputKeys = new Set([
  'input.value', 'input.mime_type', 'raw.input', 'ai.prompt', 'ai.prompt.messages', 'ai.prompt.tools',
  'gen_ai.input.messages', 'gen_ai.system_instructions', 'gen_ai.tool.call.arguments',
  'gen_ai.tool.definitions', 'ai.toolCall.args', 'tool.parameters',
  'ai.value', 'ai.values', 'embedding.text', 'ai.documents', 'ai.request.text',
  'reranker.query', 'reranker.input_documents', 'llm.prompt_template.variables',
  'gen_ai.prompt.template.variables', 'ai.schema', 'ai.schema.description',
  'ai.decision.state', 'ai.decision.questions',
]);
const outputKeys = new Set([
  'output.value', 'output.mime_type', 'raw.output', 'gen_ai.output.messages',
  'ai.response.text', 'ai.response.object', 'ai.response.toolCalls',
  'ai.response.reasoning', 'ai.toolCall.result', 'gen_ai.tool.call.result',
  'message.tool_calls', 'reranker.output_documents', 'ai.decision.answers', 'ai.ranking',
]);

/** True for the entry itself or any `<entry>.N...` child: the same family semantics as fi-core. */
function inFamily(entries: Set<string>, key: string): boolean {
  if (entries.has(key)) return true;
  for (let dot = key.indexOf('.'); dot !== -1; dot = key.indexOf('.', dot + 1)) {
    if (entries.has(key.slice(0, dot))) return true;
  }
  return false;
}

const embeddingVectorKeys = new Set(['ai.embedding', 'ai.embeddings', 'embedding.vector']);

function maskAttributes(attributes: Attributes, config: PrivacyConfig): Attributes {
  const masked: Attributes = {};
  for (const [key, value] of Object.entries(attributes)) {
    // All request headers are excluded, not only a denylist of known credentials:
    // private gateways can use arbitrary custom authentication header names.
    if (key.toLowerCase().startsWith('ai.request.headers.')) continue;
    if (config.hideErrorDetails && /(?:^|\.)(?:exception\.(?:message|stacktrace)|error\.message|error_message)$/.test(key)) continue;
    if (config.hideInputs && (inFamily(inputKeys, key) || /^embedding\.embeddings\.\d+\.embedding\.text$/.test(key))) continue;
    if (config.hideOutputs && inFamily(outputKeys, key)) continue;
    if ((config.hideInputs || config.hideOutputs) && (key === 'ai.response.providerMetadata' || key.startsWith('ai.response.providerMetadata.'))) continue;
    if ((config.hideOutputs || config.hideEmbeddingVectors) &&
      (inFamily(embeddingVectorKeys, key) ||
        /^embedding\.embeddings\.\d+\.embedding\.vector$/.test(key))) continue;
    if (value !== undefined) masked[key] = value;
  }
  return masked;
}

/** Normalize and sanitize an export-only view, leaving other processors' spans alone. */
export function prepareSpanForExport(span: ReadableSpan, config: PrivacyConfig): ReadableSpan {
  const attributes = { ...span.attributes };
  const spanContext = span.spanContext();
  // Copy the public ReadableSpan contract explicitly. Spreading the SDK span
  // would lose getters and could retain SDK-private references to raw content.
  const view: ReadableSpan = {
    name: span.name,
    kind: span.kind,
    spanContext: () => spanContext,
    parentSpanId: span.parentSpanId,
    startTime: span.startTime,
    endTime: span.endTime,
    duration: span.duration,
    ended: span.ended,
    resource: span.resource,
    instrumentationLibrary: span.instrumentationLibrary,
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
    attributes,
    status: config.hideErrorDetails ? { code: span.status.code } : { ...span.status },
    events: span.events.map(event => ({
      ...event, attributes: event.attributes ? maskAttributes(event.attributes, config) : undefined,
    })),
    links: span.links.map(link => ({
      ...link, attributes: link.attributes ? maskAttributes(link.attributes, config) : undefined,
    })),
  };
  addFIAttributesToSpan(view);
  const masked = maskAttributes(attributes, config);
  for (const key of Object.keys(attributes)) delete attributes[key];
  Object.assign(attributes, masked);
  return view;
}
