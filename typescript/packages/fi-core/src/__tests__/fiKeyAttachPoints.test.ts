import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

/**
 * Every place the SDK reads Future AGI keys from the environment, or names the key headers, is pinned here
 * with its role. A new site fails this test until it is added to the table, and a site that attaches keys
 * to a request must go through the one shared guard (fiEnvKeysFor, which calls guardFiKeys from
 * fi-endpoint.ts). Roles:
 *   reader  the only function that reads FI_API_KEY / FI_SECRET_KEY, and it is called only by the guard
 *   guard   reads the keys through the reader and refuses an endpoint the keys may not be sent to
 *   attach  puts keys on a request; must obtain the environment's keys through the guard
 *   names   mentions the key names to match, validate or report them; reads and attaches nothing
 */
const PINNED: Record<string, "reader" | "guard" | "attach" | "names"> = {
  "otel.ts::getEnvFiAuthHeader": "reader",
  "otel.ts::fiEnvKeysFor": "guard",
  "otel.ts::resolveFiHeaders": "attach",
  "otel.ts::checkCustomEvalConfigExists": "attach",
  "otel.ts::checkCustomEvalTemplateExists": "attach",
  "otel.ts::GRPCSpanExporter.constructor": "names",
  "fi-key-pair.ts::hasCompleteKeyPair": "names",
  "fi-route.ts::createFiRouteSpanExporter": "names",
};

const SRC = join(__dirname, "..");
const TOKEN = /FI_API_KEY|FI_SECRET_KEY|x-api-key|x-secret-key|getEnvFiAuthHeader|fiEnvKeysFor/i;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    if (entry.isDirectory()) return entry.name === "__tests__" || entry.name === "generated" ? [] : sourceFiles(join(dir, entry.name));
    return entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts") ? [join(dir, entry.name)] : [];
  });
}

interface Fn { site: string; start: number; end: number }
/** Top-level functions and class methods of a file, each as the span of lines up to the next declaration. */
function functions(text: string[], file: string): Fn[] {
  const found: Fn[] = [];
  let klass = "";
  text.forEach((line, i) => {
    const cls = /^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)/.exec(line);
    if (cls) { klass = cls[1]; return; }
    if (/^\S/.test(line) && !/^(?:export\s+)?(?:async\s+)?function\s/.test(line)) { if (/^[}\]]/.test(line) && klass) klass = ""; }
    const fn = /^(?:export\s+)?(?:async\s+)?function\s+(\w+)/.exec(line);
    if (fn) { found.push({ site: `${file}::${fn[1]}`, start: i, end: text.length }); return; }
    const method = klass ? /^ {2}(?:(?:private|public|protected|static|async|readonly)\s+)*(\w+)\s*(?:<[^>]*>)?\(/.exec(line) : null;
    if (method && !["if", "for", "while", "switch", "catch", "return"].includes(method[1])) found.push({ site: `${file}::${klass}.${method[1]}`, start: i, end: text.length });
  });
  found.forEach((f, i) => { if (found[i + 1]) f.end = found[i + 1].start; });
  return found;
}

function scan() {
  const sites = new Map<string, string>(); // site -> body text
  const calls: Array<{ fn: string; line: string }> = [];
  for (const path of sourceFiles(SRC)) {
    const file = relative(SRC, path);
    const text = readFileSync(path, "utf8").split("\n");
    const fns = functions(text, file);
    text.forEach((line, i) => {
      const code = line.replace(/\/\/.*$/, "");
      if (/^\s*(\*|\/\*)/.test(code) || !TOKEN.test(code)) return;
      const owner = [...fns].reverse().find(f => f.start <= i);
      const site = owner?.site ?? `${file}::<top level>`;
      sites.set(site, owner ? text.slice(owner.start, owner.end).join("\n") : "");
      if (/getEnvFiAuthHeader\(/.test(code) && !/function\s+getEnvFiAuthHeader/.test(code)) calls.push({ fn: site, line: code.trim() });
    });
  }
  return { sites, calls };
}

/** The functions that apply the guard: obtaining the environment's keys, resolving an exporter's final headers, and a gRPC exporter's channel. */
const GUARD_CALLERS = ["otel.ts::fiEnvKeysFor", "otel.ts::grpcChannelCredentials", "otel.ts::headersForEndpoint"];
function guardCallers(): string[] {
  const found = new Set<string>();
  for (const path of sourceFiles(SRC)) {
    const file = relative(SRC, path);
    const text = readFileSync(path, "utf8").split("\n");
    const fns = functions(text, file);
    text.forEach((line, i) => {
      const code = line.replace(/\/\/.*$/, "");
      if (/^\s*(\*|\/\*)/.test(code) || !/guardFiKeys\(/.test(code) || /function\s+guardFiKeys/.test(code)) return;
      found.add([...fns].reverse().find(f => f.start <= i)?.site ?? `${file}::<top level>`);
    });
  }
  return [...found].sort();
}

describe("every place Future AGI keys are read or attached goes through the shared guard", () => {
  test("the guard is applied in exactly the pinned places", () => {
    expect(guardCallers()).toEqual(GUARD_CALLERS);
  });

  const { sites, calls } = scan();

  test("the set of sites is exactly the pinned one", () => {
    expect([...sites.keys()].sort()).toEqual(Object.keys(PINNED).sort());
  });
  test("only the guard calls the environment reader", () => {
    expect(calls.map(c => c.fn)).toEqual(["otel.ts::fiEnvKeysFor"]);
  });
  test("the guard reads through the reader and applies guardFiKeys", () => {
    const body = sites.get("otel.ts::fiEnvKeysFor") ?? "";
    expect(body).toMatch(/getEnvFiAuthHeader\(/);
    expect(body).toMatch(/guardFiKeys\(/);
  });
  test.each(Object.entries(PINNED).filter(([, role]) => role === "attach"))("%s obtains the environment's keys through the guard", site => {
    expect(sites.get(site) ?? "").toMatch(/fiEnvKeysFor\(/);
  });
  test.each(Object.entries(PINNED).filter(([, role]) => role === "names"))("%s reads no key from the environment", site => {
    expect(sites.get(site) ?? "").not.toMatch(/getEnv\(\s*["'](?:FI_API_KEY|FI_SECRET_KEY)|process\.env\.FI_(?:API|SECRET)_KEY|getEnvFiAuthHeader\(/);
  });
  test("the exporters resolve their headers through headersForEndpoint, which applies the guard to the final pair", () => {
    const text = readFileSync(join(SRC, "otel.ts"), "utf8");
    const body = /function headersForEndpoint[\s\S]*?\n}\n/.exec(text)?.[0] ?? "";
    expect(body).toMatch(/guardFiKeys\(/);
    expect((text.match(/headersForEndpoint\(/g) ?? []).length).toBeGreaterThanOrEqual(4); // its definition and the three exporters
  });
});
