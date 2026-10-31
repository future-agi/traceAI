import { createHash, createHmac } from "node:crypto";

/** Frozen v1 identity encoding. Provenance, content and local cursors never enter it. */
export type Anchor = string | null | readonly Anchor[];
export type WorkKind =
  | "turn"
  | "request"
  | "attempt"
  | "tool"
  | "activity"
  | "compaction";
export function canonical(value: Anchor): string {
  let nodes = 0;
  function check(v: Anchor, depth: number): void {
    if (++nodes > 256 || depth > 8) throw new Error("INVALID_ANCHOR");
    if (v === null) return;
    if (typeof v === "string") {
      if (
        !v ||
        v.length > 4096 ||
        /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(
          v,
        )
      )
        throw new Error("INVALID_ANCHOR");
      return;
    }
    if (!Array.isArray(v)) throw new Error("INVALID_ANCHOR");
    for (const part of v) check(part, depth + 1);
  }
  check(value, 0);
  return JSON.stringify(value);
}
function digest(key: Uint8Array, value: Anchor): string {
  if (key.byteLength !== 32) throw new Error("HELD_IDENTITY_KEY");
  return createHmac("sha256", key).update(canonical(value)).digest("hex");
}
export function sessionId(
  key: Uint8Array,
  namespace: string,
  session: Anchor,
): string {
  return digest(key, ["fi-session-v1", namespace, session]);
}
export function workId(
  key: Uint8Array,
  namespace: string,
  kind: WorkKind,
  session: Anchor,
  turn: Anchor,
  work?: Anchor,
): string {
  if (kind !== "turn" && work === undefined) throw new Error("HELD_IDENTITY");
  return digest(
    key,
    kind === "turn"
      ? ["fi-work-v1", namespace, kind, session, turn]
      : ["fi-work-v1", namespace, kind, session, turn, work!],
  );
}
export function usageId(
  key: Uint8Array,
  work: string,
  scope: string,
  metric: string,
  counter: Anchor,
): string {
  return digest(key, ["fi-usage-v1", work, scope, metric, counter]);
}
export function opaqueId(
  key: Uint8Array,
  domain: string,
  anchor: string,
): string {
  return digest(key, ["fi-external-v1", domain, anchor]);
}
function shortened(domain: string, id: string, bytes: number): string {
  if (!/^[0-9a-f]{64}$/.test(id)) throw new Error("INVALID_WORK_ID");
  let preimage = canonical([domain, id]);
  for (;;) {
    const value = createHash("sha256")
      .update(preimage)
      .digest("hex")
      .slice(0, bytes * 2);
    if (!/^0+$/.test(value)) return value;
    preimage += "nonzero";
  }
}
export const traceId = (turn: string): string =>
  shortened("fi-otlp-trace-v1", turn, 16);
export const spanId = (work: string): string =>
  shortened("fi-otlp-span-v1", work, 8);
export function validNativeId(id: unknown, bytes: number): id is string {
  return (
    typeof id === "string" &&
    new RegExp(`^[0-9a-f]{${bytes * 2}}$`).test(id) &&
    !/^0+$/.test(id)
  );
}
