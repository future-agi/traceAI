import { isIP } from "node:net";
import { providerForDestination } from "./wire/core";

export type OutgoingPropagationMode = "all" | "none" | "default";
export interface HostRule { readonly host: string; readonly subdomains: boolean; readonly deny: boolean }
export interface OutgoingPolicy {
  readonly mode: OutgoingPropagationMode; readonly valid: boolean;
  readonly rules: readonly HostRule[]; readonly allowRules: number; readonly denyRules: number;
}
const invalid: OutgoingPolicy = Object.freeze({ mode: "none", valid: false, rules: Object.freeze([]), allowRules: 0, denyRules: 0 });
/** DNS normalization only; this function never resolves a host or reads environment. */
export function normalizePropagationHost(input: string): string | undefined {
  try {
    if (typeof input !== "string" || !input || input.length > 1024 || /[\s/@\\?#%*]/u.test(input)) return;
    if (input.startsWith("[")) {
      if (!input.endsWith("]") || isIP(input.slice(1, -1)) !== 6) return;
      return new URL(`http://${input}/`).hostname.toLowerCase();
    }
    if (input.includes(":")) return;
    // WHATWG IDNA also maps Unicode full stops. Strip exactly one terminal dot
    // AFTER that mapping; a second dot remains invalid. This DNS-policy key is
    // not an HTTP origin identity (dotted and undotted URL origins differ).
    const mapped = new URL(`http://${input}/`).hostname.toLowerCase();
    const canonical = mapped.endsWith(".") ? mapped.slice(0, -1) : mapped;
    if (!canonical || canonical.endsWith(".")) return;
    // Preserve the existing refusal of legacy/noncanonical IPv4 spellings.
    const literal = input.endsWith(".") ? input.slice(0, -1) : input;
    if (isIP(canonical)) return isIP(literal) === 4 && literal === canonical ? canonical : undefined;
    if (canonical.length > 253 || !canonical.split(".").every(label => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label))) return;
    return canonical;
  } catch { return; }
}
/** Invalid input disables the entire policy: silently dropping a deny is unsafe. */
export function compileOutgoingPolicy(rawMode: unknown, rawHosts: unknown): OutgoingPolicy {
  try {
    if (rawMode !== undefined && typeof rawMode !== "string") return invalid;
    const mode = typeof rawMode === "string" && rawMode.trim() ? rawMode.trim() : "default";
    if (mode !== "all" && mode !== "none" && mode !== "default") return invalid;
    if (typeof rawHosts === "string" && Buffer.byteLength(rawHosts, "utf8") > 16384) return invalid;
    let entries: unknown[];
    if (rawHosts === undefined || typeof rawHosts === "string" && !rawHosts.trim()) entries = [];
    else if (typeof rawHosts === "string") entries = rawHosts.split(",");
    else if (Array.isArray(rawHosts)) entries = rawHosts;
    else return invalid;
    if (entries.length > 256) return invalid;
    let bytes = 0;
    const rules: HostRule[] = [];
    for (const entry of entries) {
      if (typeof entry !== "string" || (bytes += Buffer.byteLength(entry, "utf8") + 1) > 16384) return invalid;
      let text = entry.trim();
      const deny = text.startsWith("-");
      if (deny || text.startsWith("+")) text = text.slice(1);
      const subdomains = text.startsWith("*.");
      if (subdomains) text = text.slice(2);
      const host = normalizePropagationHost(text);
      if (!host || subdomains && (host.startsWith("[") || isIP(host))) return invalid;
      rules.push(Object.freeze({ host, subdomains, deny }));
    }
    const denyRules = rules.filter(rule => rule.deny).length;
    return Object.freeze({ mode, valid: true, rules: Object.freeze(rules), denyRules, allowRules: rules.length - denyRules });
  } catch { return invalid; }
}
export function allowsOutgoingDestination(policy: OutgoingPolicy, destination: URL): boolean {
  try {
    if (!policy.valid || policy.mode === "none" || !["http:", "https:"].includes(destination.protocol)) return false;
    const host = normalizePropagationHost(destination.hostname);
    if (!host || destination.username || destination.password) return false;
    // Catalog hosts and effective provider ports are unconditional safety vetoes.
    // Modes and positive host rules can narrow admission, never waive this gate.
    if (providerForDestination(host, Number(destination.port || (destination.protocol === "https:" ? 443 : 80))) !== undefined) return false;
    const matches = (rule: HostRule) => rule.subdomains ? host.length > rule.host.length + 1 && host.endsWith(`.${rule.host}`) : host === rule.host;
    if (policy.rules.some(rule => rule.deny && matches(rule))) return false;
    if (policy.allowRules) return policy.rules.some(rule => !rule.deny && matches(rule));
    return true; // default and all both retain the mandatory catalog exclusions.
  } catch { return false; }
}
