/**
 * Content-type aware payload capture and best-effort result extraction, matching
 * python/frameworks/sagemaker (`_payload.py`).
 *
 * Everything here is pure and defensive: a payload shape that is not recognised yields
 * `undefined`, never an exception that could reach the caller of `send`.
 */

/** Longest text recorded for one payload. JSON is only parsed below the parse limit. */
export const TEXT_LIMIT = 32768;
export const PARSE_LIMIT = 1 << 20;

export type Kind = "json" | "text" | "binary";
type Dict = Record<string, unknown>;

const JSON_TYPES = new Set([
  "application/json", "application/jsonlines", "application/jsonl", "application/x-ndjson", "application/x-jsonlines",
]);
const TEXT_TYPES = new Set([
  "application/xml", "application/x-yaml", "application/yaml", "application/x-www-form-urlencoded",
]);
const REQUEST_PARAMETER_KEYS = [
  "max_tokens", "max_new_tokens", "max_completion_tokens", "temperature", "top_p", "top_k", "n",
  "stop", "seed", "frequency_penalty", "presence_penalty", "repetition_penalty",
];

export const dict = (value: unknown): Dict | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Dict) : undefined;
const str = (value: unknown): string | undefined => (typeof value === "string" && value ? value : undefined);
const head = (value: unknown): Dict => (Array.isArray(value) && value.length ? dict(value[0]) ?? {} : {});

export function baseType(contentType: unknown): string {
  return typeof contentType === "string" ? contentType.split(";", 1)[0].trim().toLowerCase() : "";
}

/** JSON or text only when the ContentType says so; everything else, including none, is binary. */
export function classify(contentType: unknown): Kind {
  const base = baseType(contentType);
  if (!base) return "binary";
  if (JSON_TYPES.has(base) || base.endsWith("+json")) return "json";
  if (base.startsWith("text/") || TEXT_TYPES.has(base) || base.endsWith("+xml")) return "text";
  return "binary";
}

export function binaryNote(contentType: unknown, size?: number): string {
  const kind = baseType(contentType) || "unknown content type";
  return size === undefined ? `[binary content: ${kind}]` : `[binary content: ${kind}, ${size} bytes]`;
}

export const clip = (text: string): string => text.slice(0, TEXT_LIMIT);

export interface Captured {
  value: string;
  mimeType: string;
  parsed?: unknown;
  /** True when the value is the body itself rather than a note about it. */
  isBody: boolean;
}

const isBytes = (data: unknown): data is Uint8Array => data instanceof Uint8Array;

/** Describe a bytes/string body; `undefined` for a body that cannot be described. */
export function captureBody(data: unknown, contentType: unknown): Captured | undefined {
  const kind = classify(contentType);
  if (typeof data === "string" || isBytes(data)) {
    const size = typeof data === "string" ? Buffer.byteLength(data) : data.byteLength;
    if (kind === "binary") return { value: binaryNote(contentType, size), mimeType: "text/plain", isBody: false };
    const text = typeof data === "string" ? data : new TextDecoder("utf-8").decode(data);
    let parsed: unknown;
    if (kind === "json" && size <= PARSE_LIMIT) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = undefined;
      }
    }
    return { value: clip(text), mimeType: parsed !== undefined ? "application/json" : "text/plain", parsed, isBody: true };
  }
  if (data === undefined || data === null) return undefined;
  if (kind === "binary") return { value: binaryNote(contentType), mimeType: "text/plain", isBody: false };
  return { value: "[streaming body not captured]", mimeType: "text/plain", isBody: false };
}

export function requestParameters(parsed: unknown): Dict | undefined {
  const body = dict(parsed);
  if (!body) return undefined;
  const nested = dict(body.parameters);
  if (nested && Object.keys(nested).length) return nested;
  const found: Dict = {};
  for (const key of REQUEST_PARAMETER_KEYS) if (body[key] !== undefined && body[key] !== null) found[key] = body[key];
  return Object.keys(found).length ? found : undefined;
}

const count = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
const first = (mapping: Dict, ...names: string[]): number | undefined => {
  for (const name of names) {
    const n = count(mapping[name]);
    if (n !== undefined) return n;
  }
  return undefined;
};

export class Usage {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  model?: string;

  merge(other: Usage): void {
    for (const name of ["inputTokens", "outputTokens", "totalTokens", "model"] as const) {
      const value = other[name];
      if (value !== undefined) (this as Record<string, unknown>)[name] = value;
    }
  }

  complete(): void {
    if (this.totalTokens === undefined && this.inputTokens !== undefined && this.outputTokens !== undefined) {
      this.totalTokens = this.inputTokens + this.outputTokens;
    }
  }
}

/** Token counts from the common response shapes; unknown shapes give an empty Usage. */
export function usageOf(value: unknown): Usage {
  const usage = new Usage();
  const obj = Array.isArray(value) ? head(value) : dict(value);
  if (!obj) return usage;
  usage.model = str(obj.model);
  const counts = dict(obj.usage) ?? {};
  // OpenAI-compatible, Anthropic-style and Bedrock Converse-style usage objects.
  usage.inputTokens = first(counts, "prompt_tokens", "input_tokens", "inputTokens");
  usage.outputTokens = first(counts, "completion_tokens", "output_tokens", "outputTokens");
  usage.totalTokens = first(counts, "total_tokens", "totalTokens");
  // Llama/Mistral/Titan-on-Bedrock style top-level counts.
  usage.inputTokens ??= first(obj, "prompt_token_count", "inputTextTokenCount");
  usage.outputTokens ??= first(obj, "generation_token_count") ?? first(head(obj.results), "tokenCount");
  // Hugging Face TGI details.
  const details = dict(obj.details) ?? {};
  usage.outputTokens ??= first(details, "generated_tokens");
  if (usage.inputTokens === undefined && Array.isArray(details.prefill) && details.prefill.length) {
    usage.inputTokens = details.prefill.length;
  }
  usage.complete();
  return usage;
}

function joinTextBlocks(blocks: unknown): string | undefined {
  if (!Array.isArray(blocks)) return undefined;
  const parts = blocks.map((b) => dict(b)?.text).filter((t): t is string => typeof t === "string");
  return parts.join("") || undefined;
}

function choiceText(choices: unknown): string | undefined {
  const choice = head(choices);
  const message = dict(choice.message) ?? {};
  for (const candidate of [message.content, choice.text, dict(choice.delta)?.content]) {
    if (typeof candidate === "string" && candidate) return candidate;
    const joined = joinTextBlocks(candidate);
    if (joined) return joined;
  }
  return undefined;
}

/** The generated text from the common response shapes, or `undefined`. */
export function textOf(value: unknown): string | undefined {
  const obj = Array.isArray(value) ? head(value) : dict(value);
  if (!obj) return undefined;
  const direct = str(obj.generated_text) ?? choiceText(obj.choices);
  if (direct) return direct;
  for (const key of ["generation", "completion", "output_text", "text"]) {
    const text = str(obj[key]);
    if (text) return text;
  }
  if (Array.isArray(obj.outputs) && obj.outputs.length) {
    const text = str(obj.outputs[0]) ?? str(dict(obj.outputs[0])?.text);
    if (text) return text;
  }
  const content = joinTextBlocks(obj.content) ?? str(obj.content);
  if (content) return content;
  if (Array.isArray(obj.generated_texts) && obj.generated_texts.length) {
    const text = str(obj.generated_texts[0]);
    if (text) return text;
  }
  return str(head(obj.results).outputText);
}
