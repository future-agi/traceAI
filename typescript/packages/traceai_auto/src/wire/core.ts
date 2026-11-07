import type { Attributes } from "@opentelemetry/api";
import protocols from "./wire-protocols.json";
import { decodeBytes, latin1, fromLatin1 } from "./bytes";
import { Arrival, jsonFrames, streamFrames, streamingMilestones } from "./stream-timing";
import { credentialSecrets, discoverSecrets, MAX_DISCOVERY_BODY_BYTES, scrubAttribute, scrubText } from "./privacy-runtime";

// This catalog is intentionally shared, not a second hand-maintained endpoint list.
interface Endpoint {
  id: string; methods: string[]; path: string; format: string; operation: string;
  span_kind: string; providers?: string[]; exclude_providers?: string[]; only_when?: string; default_provider?: string;
}
interface Provider {
  name: string; hosts?: string[]; host_suffixes?: string[]; host_prefixes?: string[];
  ports?: number[]; match?: string;
}
interface Catalog { default_provider: string; providers: Provider[]; endpoints: Endpoint[] }
const catalog = protocols as Catalog;
export function loadCatalog(): Catalog {
  return catalog;
}
export interface Match {
  endpoint: Endpoint; provider: string; host: string; port: number;
  groups: Record<string, string>; query: URLSearchParams; knownProvider: boolean;
}
export function match(method: string, url: string): Match | undefined {
  try {
    const u = new URL(url), cat = loadCatalog();
    const host = u.hostname.toLowerCase(), port = Number(u.port || (u.protocol === "https:" ? 443 : 80));
    const candidates = cat.providers.map(provider => ({ provider, checks: [
      provider.hosts?.length ? provider.hosts.includes(host) : undefined,
      provider.host_suffixes?.length ? provider.host_suffixes.some(s => host.endsWith(s)) : undefined,
      provider.host_prefixes?.length ? provider.host_prefixes.some(s => host.startsWith(s)) : undefined,
      provider.ports?.length ? provider.ports.includes(port) : undefined,
    ] })).filter(({ provider, checks }) => provider.match !== "all" || checks.filter(value => value !== undefined).every(Boolean));
    // Exact hosts outrank suffixes, prefixes and generic local ports, regardless of catalog order.
    const hostProvider = [0, 1, 2, 3].map(tier => candidates.find(({ checks }) => checks[tier])?.provider.name).find(Boolean);
    for (const endpoint of cat.endpoints) {
      const provider = hostProvider ?? endpoint.default_provider ?? cat.default_provider;
      if (!endpoint.methods.includes(method.toUpperCase()) ||
          (endpoint.providers && !endpoint.providers.includes(provider)) ||
          endpoint.exclude_providers?.includes(provider)) continue;
      const found = new RegExp(endpoint.path).exec(u.pathname);
      if (found) return { endpoint, provider, host, port, knownProvider: Boolean(hostProvider), query: u.searchParams,
        groups: Object.fromEntries(Object.entries(found.groups ?? {}).map(([k, v]) => [k, decodeURIComponent(v)])) };
    }
  } catch { /* Invalid URLs are not AI traffic. */ }
  return undefined;
}
export interface FilePart { name: string; filename?: string; content_type?: string; size?: number }
export interface RequestData {
  method: string; url: string; headers: Record<string, string>; body?: Uint8Array;
  fields?: Record<string, string>; files?: FilePart[]; bodySize?: number; truncated?: boolean; unavailable?: boolean; secrets?: string[];
}
export interface ResponseData {
  status?: number; headers: Record<string, string>; body?: Uint8Array; size: number;
  truncated?: boolean; incomplete?: boolean; unavailable?: boolean; undecodedEncoding?: string; firstChunkSeconds?: number; error?: Error; audioOutputBytes?: number; alreadyDecoded?: boolean;
  arrivalTimes?: Arrival[]; timingIncomplete?: boolean; firstTextSeconds?: number; eventArrivalSeconds?: number[];
}
export interface SpanResult { name: string; attributes: Attributes; ok: boolean; description?: string }
// Private side channel: request credentials must also scrub context/resource/event
// attributes added after wire parsing, without becoming serializable span data.
// null means discovery was incomplete: the exporter must drop content, not the span.
const bodySecrets = new WeakMap<RequestData, readonly string[] | null>();
/** Discover materialized credentials before a capture cap discards their source.
 * Retain only bounded private secrets, never the discarded request body.
 */
export function retainBodySecrets(req: RequestData, body: string | Uint8Array | Iterable<unknown>): void {
  try {
    if ((typeof body === "string" || body instanceof Uint8Array) && body.length > MAX_DISCOVERY_BODY_BYTES) throw new Error("Privacy discovery body budget");
    bodySecrets.set(req, discoverSecrets(typeof body === "string" ? [body] : body instanceof Uint8Array ? [decodeBytes(body)] : body));
  } catch { bodySecrets.set(req, null); }
}
/** @internal Consume retained discovery during outgoing observation, not response completion. */
export function retainedBodySecrets(req: RequestData): readonly string[] | null | undefined { return bodySecrets.get(req); }
const resultSecrets = new WeakMap<SpanResult, readonly string[] | null>();
export function captureSecrets(result: SpanResult): readonly string[] | null { return resultSecrets.has(result) ? resultSecrets.get(result)! : []; }
export function header(headers: Record<string, string>, name: string): string {
  return Object.entries(headers).find(([k]) => k.toLowerCase() === name)?.[1]?.split(";")[0].trim().toLowerCase() ?? "";
}
export function isBinary(type: string): boolean {
  return /^(audio|video|image)\//.test(type) || ["application/octet-stream", "application/vnd.amazon.eventstream"].includes(type);
}
function json(data?: Uint8Array | string): any {
  try { return JSON.parse(typeof data === "string" ? data : data ? decodeBytes(data) : ""); } catch { return undefined; }
}
export function parseSSE(data: Uint8Array): [string | undefined, any][] {
  return streamFrames(data, true).map(frame => [frame.event, frame.payload]);
}
interface Body { kind: string; data?: any; text?: string; payloads: any[] }
function decode(resp: ResponseData): Body {
  const type = header(resp.headers, "content-type");
  if (isBinary(type)) return { kind: "binary", payloads: [] };
  if (!resp.body || resp.truncated) return { kind: "empty", payloads: [] };
  if (type === "text/event-stream") return { kind: "sse", payloads: parseSSE(resp.body).filter(([, payload]) => object(payload)).map(([event, payload]) => event && payload.type === undefined ? { ...payload, type: event } : payload) };
  const text = decodeBytes(resp.body);
  if (["application/x-ndjson", "application/jsonl", "application/ndjson"].includes(type)) {
    return { kind: "ndjson", payloads: streamFrames(resp.body, false, Infinity, !resp.error && !resp.incomplete && !resp.truncated).map(frame => frame.payload).filter(object) };
  }
  const data = json(resp.body);
  if (data !== undefined) return { kind: "json", data, payloads: [] };
  if (/^\s*\[/.test(text)) {
    const members = jsonFrames(resp.body).map(frame => frame.payload);
    return members.length ? { kind: "json", data: members, payloads: [] } : { kind: "empty", payloads: [] };
  }
  const lines = text.split(/\r?\n/).filter(x => x.trim());
  const payloads = lines.map(x => json(x)).filter(object);
  if (lines.length > 1 && payloads.length) return { kind: "ndjson", payloads };
  return { kind: "text", text, payloads: [] };
}
function object(value: any): boolean { return value !== null && typeof value === "object" && !Array.isArray(value); }
function list(value: any): any[] { return Array.isArray(value) ? value : []; }
function num(value: any): number | undefined { return typeof value === "number" && Number.isFinite(value) ? value : undefined; }
function int(value: any): number | undefined { const n = num(value); return n === undefined ? undefined : Math.trunc(n); }
function put(attrs: Attributes, key: string, value: any): void {
  if (value == null || value === "" || (Array.isArray(value) && !value.length)) return;
  attrs[key] = typeof value === "object" && !(Array.isArray(value) && value.every(x => typeof x === "string")) ? JSON.stringify(value) : value;
}
export function textOf(content: any): string | undefined {
  if (typeof content === "string") return content;
  if (object(content)) return typeof content.text === "string" ? content.text : textOf(content.parts);
  if (Array.isArray(content)) {
    const texts = content.flatMap(p => typeof p === "string" ? [p] : object(p) && typeof p.text === "string" &&
      [undefined, null, "text", "input_text", "output_text"].includes(p.type) ? [p.text] : []);
    return texts.length ? texts.join("\n") : undefined;
  }
  return undefined;
}
interface ToolCall { id?: string; name?: string; arguments?: string }
interface Message { role: string; content?: string; calls: ToolCall[]; tool_call_id?: string }
function message(role: string, content?: string): Message { return { role, content, calls: [] }; }
function args(value: any): string | undefined { return value == null ? undefined : typeof value === "string" ? value : JSON.stringify(value); }
function openaiMessage(raw: any): Message {
  return { role: String(raw.role || "user"), content: textOf(raw.content), tool_call_id: raw.tool_call_id,
    calls: list(raw.tool_calls).map(c => ({ id: c.id, name: c.function?.name, arguments: args(c.function?.arguments) })) };
}
function anthropicMessage(role: string, content: any): Message {
  const out = message(role);
  if (typeof content === "string") { out.content = content; return out; }
  const texts: string[] = [];
  for (const b of list(content).filter(object)) {
    if (b.type === "text" && typeof b.text === "string") texts.push(b.text);
    if (b.type === "tool_use") out.calls.push({ id: b.id, name: b.name, arguments: args(b.input) });
    if (b.type === "tool_result") { out.tool_call_id = b.tool_use_id; const t = textOf(b.content); if (t) texts.push(t); }
  }
  out.content = texts.length ? texts.join("\n") : undefined;
  return out;
}
function geminiMessage(raw: any): Message {
  const out = message(raw.role === "model" ? "assistant" : String(raw.role || "user")), texts: string[] = [];
  for (const part of list(raw.parts).filter(object)) {
    if (typeof part.text === "string" && !part.thought) texts.push(part.text);
    else if (object(part.functionCall)) out.calls.push({ id: part.functionCall.id, name: part.functionCall.name, arguments: args(part.functionCall.args) });
    else if (object(part.functionResponse)) { out.role = "tool"; out.tool_call_id = part.functionResponse.id || part.functionResponse.name; texts.push(args(part.functionResponse.response) || ""); }
  }
  out.content = texts.length ? texts.join("\n") : undefined;
  return out;
}
function bedrockMessage(raw: any): Message {
  const out = message(String(raw.role || "user")), texts: string[] = [];
  for (const block of list(raw.content).filter(object)) {
    if (typeof block.text === "string") texts.push(block.text);
    else if (object(block.toolUse)) out.calls.push({ id: block.toolUse.toolUseId, name: block.toolUse.name, arguments: args(block.toolUse.input) });
    else if (object(block.toolResult)) { out.tool_call_id = block.toolResult.toolUseId; texts.push(...list(block.toolResult.content).map(textOf).filter(Boolean) as string[]); }
  }
  out.content = texts.length ? texts.join("\n") : undefined;
  return out;
}
interface Parsed {
  requestModel?: string; responseModel?: string; responseId?: string; finish: string[];
  inputs: Message[]; outputs: Message[]; tools: any[]; params: Record<string, any>;
  usage: Record<string, number | null>; usageFamily?: "anthropic" | "gemini" | "converse";
  inputValue?: string; inputMime?: string; outputValue?: string;
  outputMime?: string; extra: Attributes; skip?: boolean;
}
function parsed(): Parsed { return { finish: [], inputs: [], outputs: [], tools: [], params: {}, usage: {}, extra: {} }; }
function usage(p: Parsed, values: Record<string, any>): void {
  // Provider events are cumulative, partial snapshots: omission retains the last
  // value; explicit zero/null replaces it. Normalize only after the final merge.
  for (const [key, value] of Object.entries(values)) if (value !== undefined) p.usage[key] = int(value) ?? null;
}
function normalizedUsage(p: Parsed): Record<string, number | null | undefined> {
  const raw = p.usage;
  const inclusive = (base: number | null | undefined, extras: (number | null | undefined)[]) =>
    typeof base !== "number" || extras.some(v => v === null) ? undefined : base + extras.reduce<number>((sum, v) => sum + (v ?? 0), 0);
  const input = p.usageFamily === "anthropic" || p.usageFamily === "converse" ? inclusive(raw.input, [raw.cache_read, raw.cache_write]) :
    p.usageFamily === "gemini" ? inclusive(raw.input, [raw.tool_input]) : raw.input;
  const output = p.usageFamily === "gemini" ? inclusive(raw.output, [raw.reasoning]) : raw.output;
  const total = typeof input === "number" && typeof output === "number" ? input + output : raw.total;
  return { ...raw, input, output, total, provider_total: typeof input === "number" && typeof output === "number" && typeof raw.total === "number" && raw.total !== total ? raw.total : undefined };
}
function common(body: any, p: Parsed): void {
  for (const key of ["temperature", "top_p", "frequency_penalty", "presence_penalty", "seed"]) if (num(body[key]) !== undefined) p.params[key] = body[key];
  for (const key of ["max_tokens", "max_completion_tokens", "max_output_tokens"]) if (int(body[key]) !== undefined) { p.params.max_tokens = body[key]; break; }
}
function mappedParams(raw: any, p: Parsed, mapping: Record<string, string>): void {
  for (const [src, dst] of Object.entries(mapping)) if (num(raw?.[src]) !== undefined) p.params[dst] = raw[src];
}
function requestParser(format: string, b: any, p: Parsed): boolean {
  if (["openai.audio_transcription", "elevenlabs.stt", "deepgram.listen", "assemblyai.transcript"].includes(format)) return true;
  if (!object(b)) return format === "bedrock.invoke";
  switch (format) {
    case "openai.chat":
      if (Array.isArray(b.messages)) p.inputs = b.messages.filter(object).map(openaiMessage);
      else if (typeof b.prompt === "string") p.inputs = [message("user", b.prompt)]; else return false;
      p.tools = list(b.tools); common(b, p); return true;
    case "openai.completions":
      if (b.prompt == null) return false;
      p.inputs = [message("user", typeof b.prompt === "string" ? b.prompt : Array.isArray(b.prompt) ? b.prompt.filter((x: any) => typeof x === "string").join("\n") : undefined)];
      common(b, p); return true;
    case "openai.responses":
      // Stored prompts may supply only a model and a prompt reference.
      if (!("input" in b) && !("model" in b)) return false;
      if (typeof b.instructions === "string") p.inputs.push(message("system", b.instructions));
      if (typeof b.input === "string") p.inputs.push(message("user", b.input));
      for (const item of list(b.input).filter(object)) {
        if (item.type === "function_call_output") p.inputs.push({ ...message("tool", textOf(item.output) || args(item.output)), tool_call_id: item.call_id });
        else if (item.type === "function_call") p.inputs.push({ ...message("assistant"), calls: [{ id: item.call_id, name: item.name, arguments: args(item.arguments) }] });
        else if ("role" in item) p.inputs.push(message(String(item.role), textOf(item.content)));
      }
      p.tools = list(b.tools); common(b, p); return true;
    case "openai.embeddings":
      if (b.input == null) return false;
      p.extra["gen_ai.request.embedding_inputs_count"] = Array.isArray(b.input) && b.input.length && !Number.isInteger(b.input[0]) ? b.input.length : 1; return true;
    case "openai.audio_speech": case "deepgram.speak": case "elevenlabs.tts": case "cartesia.tts": {
      const key = format === "openai.audio_speech" ? "input" : format === "cartesia.tts" ? "transcript" : "text";
      if (typeof b[key] !== "string") return false;
      p.inputValue = b[key]; p.inputMime = "text/plain"; p.extra["gen_ai.audio.transcript"] = b[key];
      if (format === "openai.audio_speech" && typeof b.voice === "string") put(p.extra, "gen_ai.request.voice", b.voice); return true;
    }
    case "openai.images":
      if (typeof b.prompt !== "string") return false;
      p.inputs = [message("user", b.prompt)]; return true;
    case "openai.moderations": return b.input != null;
    case "rerank":
      if (typeof b.query !== "string" || !Array.isArray(b.documents)) return false;
      p.inputs = [message("user", b.query)]; p.extra["gen_ai.rerank.documents_count"] = b.documents.length;
      put(p.extra, "gen_ai.request.top_n", int(b.top_n ?? b.top_k)); return true;
    case "anthropic.messages":
      if (!Array.isArray(b.messages)) return false;
      if (b.system) p.inputs.push(message("system", textOf(b.system)));
      p.inputs.push(...b.messages.filter(object).map((x: any) => anthropicMessage(String(x.role || "user"), x.content)));
      p.tools = list(b.tools); common(b, p); return true;
    case "gemini.generate_content":
      if (b.contents == null) return false;
      if (b.systemInstruction || b.system_instruction) p.inputs.push(message("system", textOf(b.systemInstruction || b.system_instruction)));
      if (typeof b.contents === "string") p.inputs.push(message("user", b.contents));
      else p.inputs.push(...(Array.isArray(b.contents) ? b.contents : [b.contents]).filter(object).map(geminiMessage));
      p.tools = list(b.tools);
      mappedParams(b.generationConfig || b.generation_config, p, { temperature: "temperature", topP: "top_p", maxOutputTokens: "max_tokens", seed: "seed", frequencyPenalty: "frequency_penalty", presencePenalty: "presence_penalty" }); return true;
    case "gemini.embed_content":
      if (!("content" in b) && !Array.isArray(b.requests)) return false;
      p.extra["gen_ai.request.embedding_inputs_count"] = "content" in b ? 1 : b.requests.length; return true;
    case "cohere.chat":
      if (Array.isArray(b.messages)) p.inputs = b.messages.filter(object).map(openaiMessage);
      else if (typeof b.message === "string") {
        if (typeof b.preamble === "string") p.inputs.push(message("system", b.preamble));
        p.inputs.push(...list(b.chat_history).filter(object).map(x => message(({ USER: "user", CHATBOT: "assistant", SYSTEM: "system" } as Record<string, string>)[String(x.role).toUpperCase()] || "user", textOf(x.message))));
        p.inputs.push(message("user", b.message));
      } else return false;
      p.tools = list(b.tools); common(b, p); if (num(b.p) !== undefined) p.params.top_p = b.p; return true;
    case "cohere.embed": {
      const items = b.texts || b.inputs || b.images;
      if (!Array.isArray(items)) return false;
      p.extra["gen_ai.request.embedding_inputs_count"] = items.length; return true;
    }
    case "bedrock.converse":
      if (!Array.isArray(b.messages)) return false;
      { const system = list(b.system).map(textOf).filter(Boolean).join("\n"); if (system) p.inputs.push(message("system", system)); }
      p.inputs.push(...b.messages.filter(object).map(bedrockMessage));
      mappedParams(b.inferenceConfig, p, { temperature: "temperature", topP: "top_p", maxTokens: "max_tokens" });
      p.tools = list(b.toolConfig?.tools); return true;
    case "bedrock.invoke": if (Array.isArray(b.messages)) requestParser("anthropic.messages", b, p); return true;
    case "ollama.chat": case "ollama.generate":
      if (format === "ollama.chat") {
        if (!Array.isArray(b.messages)) return false;
        p.inputs = b.messages.filter(object).map(openaiMessage); p.tools = list(b.tools);
      } else {
        if (typeof b.prompt !== "string") return false;
        if (typeof b.system === "string") p.inputs.push(message("system", b.system));
        p.inputs.push(message("user", b.prompt));
      }
      mappedParams(b.options, p, { temperature: "temperature", top_p: "top_p", num_predict: "max_tokens", seed: "seed" }); return true;
    case "ollama.embed":
      if ((b.input ?? b.prompt) == null) return false;
      p.extra["gen_ai.request.embedding_inputs_count"] = Array.isArray(b.input ?? b.prompt) ? (b.input ?? b.prompt).length : 1; return true;
    default: return ["openai.audio_transcription", "elevenlabs.stt", "deepgram.listen", "assemblyai.transcript"].includes(format);
  }
}
const CONTENT_KEYS = new Set(["messages", "input", "prompt", "contents", "system", "systemInstruction", "system_instruction", "tools", "documents", "text", "file", "transcript", "instructions", "chat_history", "message", "preamble", "texts", "inputs", "query", "content", "requests", "audio_url", "url", "file_url", "image_url"]);
export function checkRequest(m: Match, req: RequestData): Parsed | undefined {
  const p = parsed(), format = m.endpoint.format;
  if (req.unavailable || req.truncated) {
    if (!m.knownProvider) return undefined;
    p.extra[req.truncated ? "traceai.wire.truncated" : "traceai.wire.body_unavailable"] = true;
    p.requestModel = m.groups.model || m.query.get("model") || undefined;
    return p;
  }
  const multipartKey = format === "openai.audio_transcription" ? "model" : format === "elevenlabs.stt" ? "model_id" : undefined;
  if (multipartKey || req.fields !== undefined) {
    if (req.fields === undefined && req.body && header(req.headers, "content-type").startsWith("multipart/")) {
      const rawType = Object.entries(req.headers).find(([k]) => k.toLowerCase() === "content-type")?.[1] || "";
      const boundary = /boundary="?([^";]+)"?/.exec(rawType)?.[1];
      if (boundary) {
        req.fields = {}; req.files = [];
        const materializedFields: Record<string, string>[] = [];
        // This fallback is for already-materialized multipart buffers, never streamed audio.
        for (const part of latin1(req.body).split(`--${boundary}`)) {
          const split = part.indexOf("\r\n\r\n"); if (split < 0) continue;
          const head = part.slice(0, split), value = part.slice(split + 4).replace(/\r\n$/, "");
          const name = /\bname="([^"]*)"/.exec(head)?.[1]; if (!name) continue;
          const filename = /filename="([^"]*)"/.exec(head)?.[1];
          if (filename !== undefined) req.files.push({ name, filename, content_type: /content-type:\s*([^\r\n;]+)/i.exec(head)?.[1]?.trim(), size: value.length });
          else {
            materializedFields.push({ [name]: decodeBytes(fromLatin1(value)) });
            req.fields[name] = decodeBytes(fromLatin1(value.slice(0, 65536)));
          }
        }
        retainBodySecrets(req, materializedFields);
      }
    }
    const fields = req.fields ?? {};
    if (multipartKey && !fields[multipartKey]) return undefined;
    p.requestModel = multipartKey ? fields[multipartKey] : undefined;
    put(p.extra, "gen_ai.audio.language", fields.language || fields.language_code);
    p.params._json = Object.fromEntries(Object.entries(fields).filter(([k]) => !CONTENT_KEYS.has(k) && k !== multipartKey));
    const description: Record<string, any> = { ...fields };
    for (const part of req.files ?? []) description[part.name] = { filename: part.filename ?? null, content_type: part.content_type ?? null, input_bytes: part.size ?? null };
    p.inputValue = JSON.stringify(description); p.inputMime = "application/json";
    if (!requestParser(format, fields, p)) return undefined;
  } else if (isBinary(header(req.headers, "content-type"))) {
    if (!["deepgram.listen", "assemblyai.transcript"].includes(format)) return undefined;
    p.extra["gen_ai.audio.mime_type"] = header(req.headers, "content-type");
    put(p.extra, "traceai.audio.input_bytes", req.bodySize ?? req.body?.length);
  } else if (req.body !== undefined) {
    const b = json(req.body);
    if (b === undefined) { if (!req.truncated && !["deepgram.listen", "assemblyai.transcript"].includes(format)) return undefined; }
    else {
      if (!requestParser(format, b, p)) return undefined;
      if (p.inputValue === undefined) { p.inputValue = decodeBytes(req.body); p.inputMime = "application/json"; }
      if (object(b)) {
        for (const key of ["model", "model_id", "speech_model", "modelId"]) if (typeof b[key] === "string") { p.requestModel = b[key]; break; }
        if (!p.params._json) p.params._json = Object.fromEntries(Object.entries(b).filter(([k]) => !CONTENT_KEYS.has(k)));
      }
    }
  } else if (!req.truncated && !["deepgram.listen", "assemblyai.transcript"].includes(format)) return undefined;
  p.requestModel ??= m.groups.model || m.query.get("model") || undefined;
  if (format === "deepgram.listen") put(p.extra, "gen_ai.audio.language", m.query.get("language"));
  if (m.groups.voice) p.extra["gen_ai.request.voice"] = m.groups.voice;
  const file = req.files?.[0];
  if (file) { put(p.extra, "gen_ai.audio.mime_type", file.content_type); put(p.extra, "traceai.audio.input_bytes", file.size); }
  return p;
}
function openaiUsage(p: Parsed, u: any): void {
  if (!object(u)) return;
  usage(p, { input: Object.hasOwn(u, "prompt_tokens") ? u.prompt_tokens : u.input_tokens, output: Object.hasOwn(u, "completion_tokens") ? u.completion_tokens : u.output_tokens, total: u.total_tokens });
  const input = u.prompt_tokens_details || u.input_tokens_details || {}, output = u.completion_tokens_details || u.output_tokens_details || {};
  usage(p, { cache_read: input.cached_tokens, input_audio: input.audio_tokens, reasoning: output.reasoning_tokens, output_audio: output.audio_tokens });
}
function transcript(p: Parsed, text: any): void {
  if (typeof text === "string") { p.extra["gen_ai.audio.transcript"] = text; p.outputValue = text; p.outputMime = "text/plain"; }
}
function responseOutput(p: Parsed, r: any): void {
  p.responseId = r.id || p.responseId; p.responseModel = r.model || p.responseModel;
  const out = message("assistant"), texts: string[] = [];
  for (const item of list(r.output).filter(object)) {
    if (item.type === "message") { const text = textOf(item.content); if (text !== undefined) texts.push(text); }
    if (item.type === "function_call") out.calls.push({ id: item.call_id, name: item.name, arguments: args(item.arguments) });
  }
  out.content = texts.length ? texts.join("") : undefined;
  if (out.content !== undefined || out.calls.length) p.outputs = [out];
  if (r.status) p.finish = [String(r.status)]; openaiUsage(p, r.usage);
}
function responseParser(m: Match, body: Body, p: Parsed): void {
  const f = m.endpoint.format, d = object(body.data) ? body.data : {}, events = body.payloads;
  if (body.kind === "json" && !object(body.data) && !["gemini.generate_content", "bedrock.invoke"].includes(f)) return;
  switch (f) {
    case "openai.chat": case "openai.completions":
      if (body.kind === "json") {
        p.responseId = d.id; p.responseModel = d.model;
        for (const c of list(d.choices)) {
          if (c.message && Object.keys(c.message).length) p.outputs.push(openaiMessage(c.message));
          else if (typeof c.text === "string") p.outputs.push(message("assistant", c.text));
          if (c.finish_reason) p.finish.push(String(c.finish_reason));
        }
        openaiUsage(p, d.usage);
      } else if (body.kind === "sse") {
        const slots = new Map<number, { out: Message; calls: Map<number, ToolCall> }>(), finish = new Map<number, string>();
        for (const chunk of events) {
          p.responseId ||= chunk.id; p.responseModel ||= chunk.model; openaiUsage(p, chunk.usage);
          for (const c of list(chunk.choices)) {
            const i = c.index ?? 0, delta = c.delta || {};
            if (delta.role || typeof delta.content === "string" || typeof c.text === "string" || delta.tool_calls?.length) {
              if (!slots.has(i)) slots.set(i, { out: message("assistant"), calls: new Map() });
              const slot = slots.get(i)!;
              if (delta.role) slot.out.role = delta.role;
              if (typeof delta.content === "string") slot.out.content = (slot.out.content || "") + delta.content;
              if (typeof c.text === "string") slot.out.content = (slot.out.content || "") + c.text;
              for (const call of list(delta.tool_calls)) {
                const idx = call.index ?? 0, prev = slot.calls.get(idx) || {};
                prev.id ||= call.id; prev.name ||= call.function?.name;
                if (typeof call.function?.arguments === "string") prev.arguments = (prev.arguments || "") + call.function.arguments;
                slot.calls.set(idx, prev);
              }
            }
            if (c.finish_reason) finish.set(i, String(c.finish_reason));
          }
        }
        p.outputs = [...slots].sort(([a], [b]) => a - b).map(([, s]) => ({ ...s.out, calls: [...s.calls].sort(([a], [b]) => a - b).map(x => x[1]) }));
        p.finish = [...finish].sort(([a], [b]) => a - b).map(x => x[1]);
      } return;
    case "openai.responses":
      if (body.kind === "json") responseOutput(p, d);
      else if (body.kind === "sse") {
        let final: any; const deltas: string[] = [], calls = new Map<number, ToolCall>();
        for (const e of events) {
          if (object(e.response)) openaiUsage(p, e.response.usage);
          if (["response.completed", "response.incomplete", "response.failed"].includes(e.type) && object(e.response)) final = e.response;
          else if (e.type === "response.created" && object(e.response)) { p.responseId = e.response.id; p.responseModel = e.response.model; }
          else if (e.type === "response.output_text.delta" && typeof e.delta === "string") deltas.push(e.delta);
          else if (["response.output_item.added", "response.output_item.done"].includes(e.type) && e.item?.type === "function_call") {
            const index = e.output_index ?? 0, call = calls.get(index) ?? {};
            call.id = e.item.call_id ?? call.id; call.name = e.item.name ?? call.name;
            if (typeof e.item.arguments === "string" && (e.item.arguments || call.arguments === undefined)) call.arguments = e.item.arguments;
            calls.set(index, call);
          } else if (["response.function_call_arguments.delta", "response.function_call_arguments.done"].includes(e.type)) {
            const index = e.output_index ?? 0, call = calls.get(index) ?? {};
            if (e.type.endsWith(".delta") && typeof e.delta === "string") call.arguments = (call.arguments ?? "") + e.delta;
            else if (typeof e.arguments === "string") call.arguments = e.arguments;
            calls.set(index, call);
          }
        }
        if (final) responseOutput(p, final);
        if (!p.outputs.length && (deltas.length || calls.size)) p.outputs = [{ ...message("assistant", deltas.length ? deltas.join("") : undefined), calls: [...calls].sort(([a], [b]) => a - b).map(([, call]) => call) }];
      } return;
    case "openai.embeddings":
      p.responseModel = d.model;
      if (Array.isArray(d.data?.[0]?.embedding)) p.extra["gen_ai.embeddings.dimension.count"] = d.data[0].embedding.length;
      openaiUsage(p, d.usage); return;
    case "openai.audio_transcription": {
      if (body.kind === "text") { transcript(p, body.text); return; }
      let data = d;
      if (body.kind === "sse") data = events.findLast(e => e.type === "transcript.text.done") || { text: events.filter(e => e.type === "transcript.text.delta").map(e => e.delta || "").join("") };
      transcript(p, data.text); put(p.extra, "gen_ai.audio.language", typeof data.language === "string" ? data.language : undefined);
      put(p.extra, "gen_ai.audio.duration_secs", num(data.duration));
      if (data.usage?.type === "duration") p.extra["gen_ai.audio.duration_secs"] ??= num(data.usage.seconds);
      else openaiUsage(p, data.usage); return;
    }
    case "openai.audio_speech": case "deepgram.speak": case "elevenlabs.tts": case "cartesia.tts":
      if (typeof d.audio_base64 === "string") p.extra["traceai.audio.output_bytes"] = base64Length(d.audio_base64);
      else if (body.kind === "sse") p.extra["traceai.audio.output_bytes"] = events.reduce((n, e) => n + (typeof e.data === "string" && (e.type || "chunk") === "chunk" ? base64Length(e.data) : 0), 0);
      return;
    case "openai.images":
      if (body.kind !== "json") return;
      for (const item of list(d.data)) if (object(item) && "b64_json" in item) item.b64_json = "<base64 omitted>";
      p.outputValue = JSON.stringify(d); p.outputMime = "application/json"; openaiUsage(p, d.usage); return;
    case "openai.moderations":
      if (body.kind !== "json") return;
      p.responseId = d.id; p.responseModel = d.model; p.extra["traceai.moderation.flagged"] = list(d.results).some(r => !!r?.flagged);
      p.outputValue = JSON.stringify(d); p.outputMime = "application/json"; return;
    case "rerank":
      if (body.kind !== "json") return;
      p.responseId = d.id; p.responseModel = d.model;
      p.outputValue = JSON.stringify(list(d.results || d.data).filter(object).map(r => ({ index: r.index ?? null, relevance_score: r.relevance_score ?? null }))); p.outputMime = "application/json";
      usage(p, { total: d.usage?.total_tokens }); return;
    case "anthropic.messages": {
      p.usageFamily = "anthropic";
      const au = (u: any) => usage(p, { input: u?.input_tokens, output: u?.output_tokens, cache_read: u?.cache_read_input_tokens, cache_write: u?.cache_creation_input_tokens });
      if (body.kind === "json" && "content" in d) {
        p.responseId = d.id; p.responseModel = d.model; p.outputs = [anthropicMessage(String(d.role || "assistant"), d.content)];
        if (d.stop_reason) p.finish = [String(d.stop_reason)]; au(d.usage);
      } else if (body.kind === "sse") {
        const blocks = new Map<number, any>();
        for (const e of events) {
          if (e.type === "message_start") { p.responseId = e.message?.id; p.responseModel = e.message?.model; au(e.message?.usage); }
          if (e.type === "content_block_start") { const b = { ...e.content_block }; b._parts = b.type === "text" ? [b.text || ""] : []; blocks.set(e.index ?? blocks.size, b); }
          if (e.type === "content_block_delta") {
            const b = blocks.get(e.index ?? 0) || { type: "text", _parts: [] }, delta = e.delta || {};
            if (delta.type === "text_delta") b._parts.push(delta.text || "");
            if (delta.type === "input_json_delta") b._parts.push(delta.partial_json || "");
            blocks.set(e.index ?? 0, b);
          }
          if (e.type === "message_delta") { if (e.delta?.stop_reason) p.finish = [String(e.delta.stop_reason)]; au(e.usage); }
        }
        const out = message("assistant"), texts: string[] = [];
        for (const [, b] of [...blocks].sort(([a], [b]) => a - b)) {
          if (b.type === "text") texts.push(b._parts.join(""));
          if (b.type === "tool_use") out.calls.push({ id: b.id, name: b.name, arguments: b._parts.join("") || args(b.input) });
        }
        out.content = texts.join("\n") || undefined; if (out.content !== undefined || out.calls.length) p.outputs = [out];
      } return;
    }
    case "gemini.generate_content": {
      p.usageFamily = "gemini";
      const items = body.kind === "sse" ? events : Array.isArray(body.data) ? body.data : [d], out = message("assistant"), texts: string[] = [];
      for (const item of items.filter(object)) {
        p.responseModel = item.modelVersion || p.responseModel; p.responseId = item.responseId || p.responseId;
        const candidates = list(item.candidates);
        if (object(candidates[0])) {
          const msg = geminiMessage(candidates[0].content || {}); if (msg.content) texts.push(msg.content); out.calls.push(...msg.calls);
          const reasons = candidates.filter(x => x?.finishReason).map(x => String(x.finishReason)); if (reasons.length) p.finish = reasons;
        }
        const u = item.usageMetadata;
        usage(p, { input: u?.promptTokenCount, tool_input: u?.toolUsePromptTokenCount, output: u?.candidatesTokenCount, total: u?.totalTokenCount, cache_read: u?.cachedContentTokenCount, reasoning: u?.thoughtsTokenCount });
      }
      out.content = texts.join("") || undefined; if (out.content !== undefined || out.calls.length) p.outputs = [out]; return;
    }
    case "gemini.embed_content": {
      const first = d.embedding || d.embeddings?.[0];
      if (Array.isArray(first?.values)) p.extra["gen_ai.embeddings.dimension.count"] = first.values.length; return;
    }
    case "cohere.chat": {
      const cohereUsage = (r: any) => { const u = r?.usage || r?.meta || {}, t = u.tokens || u.billed_units || {}; usage(p, { input: t.input_tokens, output: t.output_tokens }); };
      if (["sse", "ndjson"].includes(body.kind)) {
        const out = message("assistant"), calls = new Map<number, ToolCall>();
        for (const event of events) {
          const type = event.type || event.event_type, delta = event.delta || {};
          if (type === "message-start") { p.responseId = event.id; out.role = delta.message?.role || out.role; }
          if (type === "content-delta" && typeof delta.message?.content?.text === "string") out.content = (out.content || "") + delta.message.content.text;
          if (type === "tool-call-start" || type === "tool-call-delta") {
            const call = delta.message?.tool_calls || {}, index = event.index ?? 0, previous = calls.get(index) || {};
            previous.id ||= call.id; previous.name ||= call.function?.name;
            if (typeof call.function?.arguments === "string") previous.arguments = (previous.arguments || "") + call.function.arguments;
            calls.set(index, previous);
          }
          if (type === "message-end") { if (delta.finish_reason) p.finish = [String(delta.finish_reason)]; cohereUsage(delta); }
          if (type === "text-generation" && typeof event.text === "string") out.content = (out.content || "") + event.text;
          if (type === "stream-end") {
            const response = event.response || {}; p.responseId ||= response.id || response.generation_id;
            if (event.finish_reason || response.finish_reason) p.finish = [String(event.finish_reason || response.finish_reason)];
            cohereUsage(response);
          }
        }
        out.calls = [...calls].sort(([a], [b]) => a - b).map(([, call]) => call);
        if (out.content !== undefined || out.calls.length) p.outputs = [out];
      } else {
        p.responseId = d.id || d.generation_id;
        if (object(d.message)) p.outputs = [{ ...openaiMessage(d.message), role: String(d.message.role || "assistant") }];
        else if (typeof d.text === "string") p.outputs = [message("assistant", d.text)];
        if (d.finish_reason) p.finish = [String(d.finish_reason)]; cohereUsage(d);
      }
      return;
    }
    case "cohere.embed": {
      const e = object(d.embeddings) ? d.embeddings.float : d.embeddings;
      if (Array.isArray(e?.[0])) p.extra["gen_ai.embeddings.dimension.count"] = e[0].length;
      usage(p, { input: d.meta?.billed_units?.input_tokens }); return;
    }
    case "bedrock.converse":
      p.usageFamily = "converse";
      if (object(d.output?.message)) p.outputs = [bedrockMessage(d.output.message)];
      if (d.stopReason) p.finish = [String(d.stopReason)];
      usage(p, { input: d.usage?.inputTokens, output: d.usage?.outputTokens, total: d.usage?.totalTokens, cache_read: d.usage?.cacheReadInputTokens, cache_write: d.usage?.cacheWriteInputTokens }); return;
    case "bedrock.invoke":
      if (body.kind === "json" && "content" in d) responseParser({ ...m, endpoint: { ...m.endpoint, format: "anthropic.messages" } }, body, p);
      else if (body.kind === "json") { p.outputValue = JSON.stringify(body.data); p.outputMime = "application/json"; } return;
    case "ollama.chat": case "ollama.generate": {
      const items = body.kind === "json" ? [d] : body.kind === "ndjson" ? events : [], out = message("assistant"), texts: string[] = [];
      for (const item of items) {
        p.responseModel = item.model || p.responseModel;
        if (object(item.message)) { const msg = openaiMessage(item.message); if (msg.content !== undefined) texts.push(msg.content); out.calls.push(...msg.calls); }
        if (typeof item.response === "string") texts.push(item.response);
        if (item.done) { if (item.done_reason) p.finish = [String(item.done_reason)]; usage(p, { input: item.prompt_eval_count, output: item.eval_count }); }
      }
      out.content = texts.join("") || undefined;
      if (out.content !== undefined || out.calls.length) p.outputs = [out]; return;
    }
    case "ollama.embed": {
      const first = d.embeddings?.[0] || d.embedding;
      if (Array.isArray(first)) p.extra["gen_ai.embeddings.dimension.count"] = first.length;
      usage(p, { input: d.prompt_eval_count }); return;
    }
    case "deepgram.listen":
      p.responseId = d.metadata?.request_id; put(p.extra, "gen_ai.audio.duration_secs", num(d.metadata?.duration));
      transcript(p, d.results?.channels?.[0]?.alternatives?.[0]?.transcript); return;
    case "elevenlabs.stt":
      transcript(p, d.text); put(p.extra, "gen_ai.audio.language", d.language_code); p.responseId = d.transcription_id; return;
    case "assemblyai.transcript":
      if (body.kind !== "json") return;
      if (m.endpoint.only_when === "completed" && !["completed", "error"].includes(d.status)) { p.skip = true; return; }
      p.responseId = d.id; if (typeof d.speech_model === "string") p.responseModel = d.speech_model;
      if (d.status) p.finish = [String(d.status)]; if (d.status === "completed") transcript(p, d.text);
      put(p.extra, "gen_ai.audio.duration_secs", num(d.audio_duration)); put(p.extra, "gen_ai.audio.language", d.language_code); return;
  }
}
// Counting base64 needs no decoding/allocation of audio bytes.
export function base64Length(value: string): number {
  const clean = value.replace(/[^A-Za-z0-9+/=]/g, "");
  return Math.max(0, Math.floor(clean.replace(/=+$/, "").length * 3 / 4));
}
function putMessages(attrs: Attributes, prefix: string, messages: Message[]): void {
  messages.forEach((msg, i) => {
    const base = `${prefix}.${i}.message`;
    put(attrs, `${base}.role`, msg.role); put(attrs, `${base}.content`, msg.content); put(attrs, `${base}.tool_call_id`, msg.tool_call_id);
    msg.calls.forEach((c, k) => { const key = `${base}.tool_calls.${k}.tool_call`; put(attrs, `${key}.id`, c.id); put(attrs, `${key}.function.name`, c.name); put(attrs, `${key}.function.arguments`, c.arguments); });
  });
}
function errorMessage(body: Body): string | undefined {
  const d = Array.isArray(body.data) ? body.data[0] : body.data;
  if (object(d)) {
    if (typeof d.error?.message === "string") return d.error.message;
    if (typeof d.error === "string") return d.error;
    for (const key of ["message", "detail", "err_msg"]) if (typeof d[key] === "string") return d[key];
    if (typeof d.detail?.message === "string") return d.detail.message;
  }
  return body.kind === "text" ? body.text?.slice(0, 500) : undefined;
}
export function build(m: Match, req: RequestData, resp: ResponseData, initial?: Parsed): SpanResult | undefined {
  try {
    const p = initial ?? checkRequest(m, req); if (!p) return undefined;
    const e = m.endpoint, body = decode(resp), ok = !resp.error && (resp.status ?? 0) < 400;
    if (e.only_when === "completed" && !resp.error && !["completed", "error"].includes(body.data?.status)) return undefined;
    if (ok || resp.error) { try { responseParser(m, body, p); } catch { /* Malformed provider data is nonfatal. */ } if (p.skip) return undefined; }
    const a: Attributes = { "gen_ai.span.kind": e.span_kind, "gen_ai.operation.name": e.operation,
      "gen_ai.provider.name": m.provider, "traceai.capture.layer": "wire", "traceai.wire.endpoint": e.id,
      "server.address": m.host, "server.port": m.port, "http.request.method": req.method.toUpperCase() };
    put(a, "http.response.status_code", resp.status);
    put(a, "gen_ai.request.model", p.requestModel); put(a, "gen_ai.response.model", p.responseModel); put(a, "gen_ai.response.id", p.responseId); put(a, "gen_ai.response.finish_reasons", p.finish);
    for (const key of ["temperature", "top_p", "max_tokens", "seed", "frequency_penalty", "presence_penalty"]) put(a, `gen_ai.request.${key}`, p.params[key]);
    if (p.params._json && Object.keys(p.params._json).length) put(a, "gen_ai.request.parameters", p.params._json);
    if (["sse", "ndjson"].includes(body.kind) || ["text/event-stream", "application/x-ndjson", "application/jsonl", "application/ndjson"].includes(header(resp.headers, "content-type")) || /(?::streamGenerateContent|:streamRawPredict|\/converse-stream|\/invoke-with-response-stream)$/.test(new URL(req.url).pathname)) {
      a["gen_ai.request.stream"] = true;
      if (resp.firstChunkSeconds !== undefined) a["gen_ai.response.time_to_first_chunk"] = Math.round(resp.firstChunkSeconds * 1e6) / 1e6;
      const timing = resp.arrivalTimes && resp.body ? streamingMilestones(resp.body, body.kind === "sse" ? true : body.kind === "json" || header(resp.headers, "content-type") === "application/json" ? "json" : false, resp.arrivalTimes, !resp.error && !resp.incomplete && !resp.truncated) : undefined;
      const firstText = resp.firstTextSeconds ?? timing?.firstText;
      if (firstText !== undefined) {
        // The event classifier excludes roles, heartbeats and usage-only chunks;
        // its content milestone includes text, reasoning, tools and audio. Keep
        // the existing spelling while exposing canonical TTFT in seconds too.
        a["gen_ai.response.time_to_first_text"] = Math.round(firstText * 1e6) / 1e6;
        a["gen_ai.response.time_to_first_token"] = Math.round(firstText * 1e6) / 1e6;
      }
      const arrivals = resp.eventArrivalSeconds ?? timing?.events;
      if (arrivals?.length) a["traceai.stream.event_arrival_seconds"] = arrivals.map(value => Math.round(value * 1e6) / 1e6);
      if (resp.timingIncomplete || timing?.incomplete || resp.truncated) { a["traceai.wire.timing_incomplete"] = true; a["traceai.wire.incomplete"] = true; }
    }
    const u = normalizedUsage(p);
    put(a, "gen_ai.usage.input_tokens", u.input); put(a, "gen_ai.usage.output_tokens", u.output);
    put(a, "gen_ai.usage.total_tokens", u.total);
    put(a, "gen_ai.usage.provider_total_tokens", u.provider_total);
    for (const [key, attr] of Object.entries({ cache_read: "cache_read_tokens", cache_write: "cache_write_tokens", reasoning: "output_tokens.reasoning", input_audio: "input_tokens.audio", output_audio: "output_tokens.audio" })) put(a, `gen_ai.usage.${attr}`, u[key]);
    // Offer canonical output before message fan-out; metadata/usage stay first and host limits still apply.
    if (p.outputValue === undefined && p.outputs.length) {
      const first = p.outputs[0];
      if (first.content) { p.outputValue = first.content; p.outputMime = "text/plain"; }
      else if (first.calls.length) { p.outputValue = JSON.stringify(first.calls.map(c => ({ id: c.id ?? null, name: c.name ?? null, arguments: c.arguments ?? null }))); p.outputMime = "application/json"; }
    }
    if (p.outputValue !== undefined) { a["output.value"] = p.outputValue; a["output.mime_type"] = p.outputMime || "text/plain"; }
    putMessages(a, "gen_ai.input.messages", p.inputs); putMessages(a, "gen_ai.output.messages", p.outputs);
    p.tools.forEach((tool, i) => { a[`gen_ai.tool.definitions.${i}.tool.json_schema`] = JSON.stringify(tool); });
    if (p.inputValue !== undefined) { a["input.value"] = p.inputValue; a["input.mime_type"] = p.inputMime || "application/json"; }
    for (const [key, value] of Object.entries(p.extra)) put(a, key, value);
    if (body.kind === "binary" && ok) {
      a["gen_ai.audio.mime_type"] ??= header(resp.headers, "content-type");
      if (!resp.unavailable && (header(resp.headers, "content-type").startsWith("audio/") || e.operation === "text_to_speech")) a["traceai.audio.output_bytes"] = resp.size;
    }
    if (resp.audioOutputBytes !== undefined && ok) a["traceai.audio.output_bytes"] = resp.audioOutputBytes;
    if (req.truncated || resp.truncated) a["traceai.wire.truncated"] = true;
    if (resp.incomplete) a["traceai.wire.incomplete"] = true;
    if (resp.unavailable) a["traceai.wire.body_unavailable"] = true;
    if (resp.undecodedEncoding) a["traceai.wire.undecoded_encoding"] = resp.undecodedEncoding;
    const model = p.requestModel || p.responseModel;
    let description: string | undefined;
    if (!ok) { a["error.type"] = resp.error ? resp.error.name : String(resp.status); description = resp.error ? resp.error.message || resp.error.name : errorMessage(body) || `HTTP ${resp.status}`; }
    let secrets = credentialSecrets(req.headers, req.secrets), privacyFailed = false;
    try {
      // Preserve raw request credentials privately before best-effort scrubbing erases
      // their fields. The same discovery policy serves wire and forwarded SDK/app spans.
      const retained = bodySecrets.get(req);
      if (retained === null || req.truncated && retained === undefined) throw new Error("Privacy request body unavailable");
      secrets = credentialSecrets({}, [...secrets, ...(retained ?? []), ...discoverSecrets([req.fields, req.fields === undefined && req.body && !isBinary(header(req.headers, "content-type")) ? decodeBytes(req.body) : undefined, req.url])]);
    } catch { privacyFailed = true; }
    let result: SpanResult;
    try {
      if (privacyFailed) throw new Error("Privacy discovery incomplete");
      for (const [key, value] of Object.entries(a)) {
        if (typeof value === "string") a[key] = scrubAttribute(value, secrets);
        else if (Array.isArray(value)) a[key] = value.map(item => typeof item === "string" ? scrubText(item, secrets) : item) as any;
      }
      result = { name: scrubText(model ? `${e.operation} ${model}` : e.operation, secrets), attributes: a, ok, description: description ? scrubAttribute(description, secrets) : undefined };
    } catch {
      // Recognition budgets must fail closed without dropping the wire span.
      privacyFailed = true; result = { name: "<redacted>", attributes: {}, ok, description: undefined };
    }
    resultSecrets.set(result, privacyFailed ? null : secrets);
    return result;
  } catch { return undefined; }
}
