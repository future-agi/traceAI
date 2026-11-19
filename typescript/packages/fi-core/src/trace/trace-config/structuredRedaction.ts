import { REDACTED_VALUE } from "./constants";

/** A structural vector policy, not a substring test that erases enclosing input. */
export function redactEmbeddingValues(value: string): string {
  let nodes = 20000;
  let text = 4 * 1024 * 1024;
  const visit = (item: unknown, depth: number, vector = false): unknown => {
    if (--nodes < 0 || depth > 32) throw new Error("Embedding privacy budget");
    if (typeof item === "string") {
      if ((text -= item.length) < 0) throw new Error("Embedding privacy text budget");
      let parsed: unknown;
      try { parsed = JSON.parse(item); } catch {
        // If malformed/embedded JSON cannot be traversed, retain the previous
        // conservative vector protection rather than exposing partial values.
        return vector || /\\*"(?:embedding|embeddings|vector|vectors)\\*"\s*:/i.test(item) ? REDACTED_VALUE : item;
      }
      const clean = visit(parsed, depth + 1, vector);
      return JSON.stringify(clean) === JSON.stringify(parsed) ? item : JSON.stringify(clean);
    }
    if (Array.isArray(item)) {
      if (vector && item.some(element => typeof element === "number" || typeof element === "boolean")) return REDACTED_VALUE;
      return item.map(element => visit(element, depth + 1, vector));
    }
    if (item !== null && typeof item === "object") {
      return Object.fromEntries(Object.entries(item).map(([key, child]) => [
        key,
        visit(child, depth + 1, /^(?:embedding|embeddings|vector|vectors)$/i.test(key) ||
          vector && !/^(?:text|input|input_text|model|index|dimensions?|count|type|mime_type)$/i.test(key)),
      ]));
    }
    return vector && (typeof item === "number" || typeof item === "boolean") ? REDACTED_VALUE : item;
  };
  return visit(value, 0) as string;
}

/**
 * Retain request configuration/identity, not input payloads. This projection does
 * not grant a credential exemption: its result must still cross ordinary
 * credential/media/cap/PII scrubbing. Unknown members fail closed.
 */
export function redactRequestMetadata(value: unknown, key: string): string {
  if (typeof value !== "string" || value.length > 4 * 1024 * 1024) return REDACTED_VALUE;
  let parsed: unknown;
  try { parsed = JSON.parse(value); } catch { return REDACTED_VALUE; }
  let nodes = 20000;
  const object = (item: unknown): item is Record<string, unknown> => item !== null && typeof item === "object" && !Array.isArray(item);
  const scalar = (item: unknown) => item === null || ["string", "number", "boolean"].includes(typeof item);
  const tool = (item: unknown, depth = 0): unknown => {
    if (--nodes < 0 || depth > 16) throw new Error("Request metadata budget");
    if (!object(item)) return REDACTED_VALUE;
    return Object.fromEntries(Object.entries(item).map(([name, child]) => {
      if (--nodes < 0) throw new Error("Request metadata budget");
      if (["type", "name", "server_label"].includes(name) && scalar(child)) return [name, child];
      if (name === "server_url" && typeof child === "string") {
        try { const url = new URL(child); return [name, /^(?:https?:)$/.test(url.protocol) ? url.origin : REDACTED_VALUE]; }
        catch { return [name, REDACTED_VALUE]; }
      }
      if ((name === "function" || name === "custom") && object(child)) return [name, tool(child, depth + 1)];
      return [name, REDACTED_VALUE];
    }));
  };
  const tools = (items: unknown) => Array.isArray(items) && items.length <= nodes ? items.map(item => tool(item)) : REDACTED_VALUE;
  if (key === "gen_ai.tool.definitions") return JSON.stringify(tools(parsed));
  if (!object(parsed)) return REDACTED_VALUE;
  const clean = Object.fromEntries(Object.entries(parsed).map(([name, child]) => {
    if (--nodes < 0) throw new Error("Request metadata budget");
    if (name === "tools") return [name, tools(child)];
    if (name === "prompt" && object(child)) return [name, Object.fromEntries(Object.entries(child).map(([part, data]) => [
      part, ["id", "version"].includes(part) && scalar(data) ? data : REDACTED_VALUE,
    ]))];
    if (["model", "temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens", "seed", "frequency_penalty", "presence_penalty", "stream", "n", "parallel_tool_calls", "store"].includes(name) && scalar(child)) return [name, child];
    return [name, REDACTED_VALUE];
  }));
  return JSON.stringify(clean);
}
