import { byteLength, decodeBytes } from "./bytes";

export interface Arrival { end: number; seconds: number }
interface Frame { event?: string; payload: any; end: number }
/** Keep source byte offsets, including CRLF and multibyte text. Event arrival is
 * the transport settlement that supplied its last byte, never the later parse time.
 */
export function streamFrames(data: Uint8Array, sse: boolean, limit = Infinity, allowEOF = true): Frame[] {
  const text = decodeBytes(data), result: Frame[] = [];
  let offset = 0, event: string | undefined, lines: string[] = [];
  const flush = () => {
    if (lines.length) {
      const raw = lines.join("\n");
      if (raw !== "[DONE]") {
        let payload: any; try { payload = JSON.parse(raw); } catch { payload = raw; }
        result.push({ event, payload, end: offset });
      }
    }
    event = undefined; lines = [];
  };
  for (const match of text.matchAll(/([^\r\n]*)(\r\n|\r|\n|$)/g)) {
    if (result.length >= limit) break;
    if (!match[0]) continue;
    const line = match[1]; offset += byteLength(match[0]);
    if (!sse) {
      if (line.trim()) {
        // JSONL permits a complete final record at EOF without a newline. Do
        // not promote a truncated record (or arbitrary trailing prose) to a frame.
        let complete = !!match[2];
        if (!complete && allowEOF) { try { JSON.parse(line); complete = true; } catch { /* Partial EOF record. */ } }
        if (complete) { lines = [line]; flush(); }
      }
      continue;
    }
    if (!line) { flush(); continue; }
    const colon = line.indexOf(":"); if (colon < 0) continue;
    const key = line.slice(0, colon), value = line.slice(colon + 1).replace(/^ /, "");
    if (key === "event") event = value;
    if (key === "data") lines.push(value);
  }
  // SSE requires its blank-line delimiter even on EOF/failure. Complete JSON
  // without that delimiter is not an observed event or usage snapshot.
  return result;
}
/** Complete JSON objects (or array members) only; a partial later member never
 * erases earlier observed output/usage. Byte offsets include split UTF-8 safely. */
export function jsonFrames(data: Uint8Array, limit = Infinity): Frame[] {
  const text = decodeBytes(data), first = text.search(/\S/), result: Frame[] = [];
  if (first < 0 || !["[", "{"].includes(text[first])) return result;
  const array = text[first] === "[";
  let start = -1, nesting = 0, quoted = false, escaped = false, last = 0, offset = 0;
  for (let i = array ? first + 1 : first; i < text.length && result.length < limit; i++) {
    const c = text[i];
    if (start < 0) {
      if (array && /[\s,]/.test(c)) continue;
      if (c !== "{" && c !== "[") break;
      start = i; nesting = 1; quoted = false; escaped = false; continue;
    }
    if (quoted) { if (escaped) escaped = false; else if (c === "\\") escaped = true; else if (c === '"') quoted = false; }
    else if (c === '"') quoted = true;
    else if (c === "{" || c === "[") nesting++;
    else if ((c === "}" || c === "]") && --nesting === 0) {
      let payload: any; try { payload = JSON.parse(text.slice(start, i + 1)); } catch { break; }
      offset += byteLength(text.slice(last, i + 1)); last = i + 1;
      result.push({ payload, end: offset }); start = -1;
      if (!array) break;
    }
  }
  return result;
}
const nonempty = (value: unknown) => typeof value === "string" && value.length > 0;
/** Text means model content (text/reasoning/tool/audio), not roles, heartbeats,
 * usage-only snapshots, created events or terminal bookkeeping. */
function contentDelta(payload: any, event?: string): boolean {
  if (!payload || typeof payload !== "object") return false;
  const type = payload.type ?? payload.event_type ?? event;
  const argumentsPresent = (value: any) => nonempty(value) || !!value && typeof value === "object" && Object.keys(value).length > 0;
  const tool = (value: any): boolean => (Array.isArray(value) ? value : [value]).some(call => {
    if (!call || typeof call !== "object") return false;
    const fn = call.function ?? call;
    return nonempty(fn.name) || [fn.arguments, fn.args, fn.parameters, fn.input, fn.code, call.arguments, call.args, call.parameters].some(argumentsPresent);
  });
  const text = (value: any, depth = 0): boolean => {
    if (depth > 16) return false;
    if (nonempty(value)) return true;
    if (Array.isArray(value)) return value.some(part => text(part, depth + 1));
    if (!value || typeof value !== "object" || typeof value.type === "string" && !/^(?:text|output_text|thinking|reasoning(?:_text)?|refusal)$/.test(value.type)) return false;
    return [value.text, value.thinking, value.reasoning, value.reasoning_content, value.refusal].some(part => text(part, depth + 1));
  };
  if (Array.isArray(payload.choices) && payload.choices.some((choice: any) => {
    const delta = choice.delta ?? {};
    return [choice.text, delta.content, delta.refusal, delta.reasoning_content, delta.reasoning, delta.audio?.transcript, delta.audio?.data].some(part => text(part)) || tool(delta.tool_calls) || tool(delta.function_call);
  })) return true;
  if (/^response\.(?:output_text|refusal|audio\.transcript|reasoning(?:_summary)?(?:_text)?|function_call_arguments|mcp_call_arguments|code_interpreter_call_code|custom_tool_call_input|audio|output_audio)\.delta$/.test(type) && nonempty(payload.delta)) return true;
  if (type === "response.output_item.added" && ["function_call", "tool_call", "custom_tool_call", "mcp_call", "code_interpreter_call"].includes(payload.item?.type) && tool(payload.item)) return true;
  if (type === "content_block_delta" && [payload.delta?.text, payload.delta?.thinking, payload.delta?.partial_json].some(nonempty)) return true;
  if (type === "content_block_start" && ([payload.content_block?.text, payload.content_block?.thinking].some(nonempty) || ["tool_use", "server_tool_use", "mcp_tool_use"].includes(payload.content_block?.type) && tool(payload.content_block))) return true;
  if (Array.isArray(payload.candidates) && payload.candidates.some((candidate: any) => candidate.content?.parts?.some((part: any) => {
    const inline = part.inlineData ?? part.inline_data;
    return nonempty(part.text) || nonempty((part.executableCode ?? part.executable_code)?.code) || tool(part.functionCall ?? part.function_call) || /^audio\//i.test(inline?.mimeType ?? inline?.mime_type ?? "") && nonempty(inline?.data);
  }))) return true;
  const bedrock = payload.contentBlockDelta?.delta, start = payload.contentBlockStart?.start;
  if ([bedrock?.text, bedrock?.reasoningContent?.text, bedrock?.toolUse?.name, start?.toolUse?.name].some(nonempty) || argumentsPresent(bedrock?.toolUse?.input)) return true;
  if (["content-delta", "content-start"].includes(type) && text(payload.delta?.message?.content)) return true;
  if (["tool-call-start", "tool-call-delta"].includes(type) && tool(payload.delta?.message?.tool_calls)) return true;
  if (type === "tool-plan-delta" && nonempty(payload.delta?.message?.tool_plan)) return true;
  if (["text-generation", "transcript.text.delta"].includes(type) && [payload.text, payload.delta].some(nonempty)) return true;
  if (type === "tool-calls-generation" && tool(payload.tool_calls)) return true;
  if (type === "tool-calls-chunk" && (nonempty(payload.text) || nonempty(payload.tool_call_delta?.text) || tool(payload.tool_call_delta) || tool(payload.tool_calls))) return true;
  if (type === "chunk" && nonempty(payload.data) || type === undefined && nonempty(payload.audio_base64)) return true;
  return type === undefined && ([payload.response, payload.thinking, payload.message?.content, payload.message?.thinking].some(nonempty) || tool(payload.message?.tool_calls));
}
export function streamingMilestones(data: Uint8Array, sse: boolean | "json", arrivals: readonly Arrival[], allowEOF = true): { events: number[]; firstText?: number; incomplete?: true } {
  const events: number[] = []; let firstText: number | undefined, index = 0, incomplete: true | undefined;
  for (const frame of sse === "json" ? jsonFrames(data, 4097) : streamFrames(data, sse, 4097, allowEOF)) {
    while (index < arrivals.length && arrivals[index].end < frame.end) index++;
    const arrival = arrivals[index]; if (!arrival || events.length >= 4096) { incomplete = true; break; }
    events.push(arrival.seconds);
    if (firstText === undefined && contentDelta(frame.payload, frame.event)) firstText = arrival.seconds;
  }
  return { events, firstText, ...(incomplete ? { incomplete } : {}) };
}
