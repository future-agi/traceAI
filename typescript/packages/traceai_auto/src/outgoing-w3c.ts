// Wire grammar is separate from privacy admission. A value can be non-secret
// but still unsafe for Node's latin1 early-header path. Only ASCII goes on wire.
const token = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const octets = /^[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*$/;
const ascii = /^[\x20-\x7e]*$/;
const ows = (value: string) => value.replace(/^[ \t]+|[ \t]+$/g, "");
function validEscapes(value: string): boolean {
  try {
    // Stricter than a literal percent: require well-formed UTF-8 escapes and
    // omit encoded controls too. Percent-encoded Unicode remains ASCII on wire.
    return !/[\x00-\x1f\x7f]/.test(decodeURIComponent(value));
  } catch { return false; }
}
export function validBaggageKey(value: string): boolean { return token.test(value) && validEscapes(value); }
export function validBaggageValue(value: string): boolean { return octets.test(value) && validEscapes(value); }
export function validBaggageMetadata(value: string): boolean {
  if (!value || value.length > 4096 || !ascii.test(value)) return false;
  return value.split(";").every(part => {
    const at = part.indexOf("="), key = ows(at < 0 ? part : part.slice(0, at)), val = at < 0 ? "" : ows(part.slice(at + 1));
    return validBaggageKey(key) && validBaggageValue(val);
  });
}
export function validBaggage(value: string): boolean {
  if (!value || value.length > 4096 || !ascii.test(value)) return false;
  const members = value.split(",");
  if (members.length > 64) return false;
  return members.every(member => {
    const semicolon = member.indexOf(";"), pair = semicolon < 0 ? member : member.slice(0, semicolon), at = pair.indexOf("=");
    return at > 0 && validBaggageKey(ows(pair.slice(0, at))) && validBaggageValue(ows(pair.slice(at + 1))) &&
      (semicolon < 0 || validBaggageMetadata(member.slice(semicolon + 1)));
  });
}
export function validTraceparent(value: string): boolean {
  const fields = /^00-([0-9a-f]{32})-([0-9a-f]{16})-(00|01)$/.exec(value);
  return !!fields && !/^0+$/.test(fields[1]) && !/^0+$/.test(fields[2]);
}
export function validTracestate(value: string): boolean {
  if (!value || value.length > 512 || !ascii.test(value)) return false;
  const members = value.split(","), keys = new Set<string>();
  if (members.length > 32) return false;
  for (const member of members) {
    const at = member.indexOf("="), key = member.slice(0, at).trim(), val = member.slice(at + 1).trim();
    if (at < 1 || keys.has(key) || !/^(?:[a-z][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,13})$/.test(key) || !val || val.length > 256 || !/^[\x20-\x2b\x2d-\x3c\x3e-\x7e]+$/.test(val)) return false;
    keys.add(key);
  }
  return true;
}
export function validOutgoingField(name: unknown, value: unknown): boolean {
  if (typeof value !== "string" || !ascii.test(value)) return false;
  if (name === "traceparent") return validTraceparent(value);
  if (name === "tracestate") return validTracestate(value);
  if (name === "baggage") return validBaggage(value);
  return false;
}
export function validOutgoingSuffix(suffix: string): boolean {
  if (!suffix || suffix.length > 8192 || !suffix.endsWith("\r\n")) return false;
  const lines = suffix.slice(0, -2).split("\r\n"), seen = new Set<string>();
  if (lines.length > 3) return false;
  for (const line of lines) {
    const at = line.indexOf(": "), name = line.slice(0, at), value = line.slice(at + 2);
    if (at < 1 || seen.has(name) || !validOutgoingField(name, value)) return false;
    seen.add(name);
  }
  return true;
}
