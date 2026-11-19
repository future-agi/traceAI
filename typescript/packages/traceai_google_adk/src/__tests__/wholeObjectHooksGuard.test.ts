/**
 * Guard for the legacy whole-object hooks (Agent / Tool / Session prototypes, Runner.prototype.run).
 *
 * Those hooks record whole run, tool and session objects. They are meant to attach to nothing: every supported
 * @google/adk version exposes none of their targets, and native tracing covers the SDK. If a future SDK adds one of
 * these methods, the hooks would start recording whole objects, so this test fails and the hook must be removed or
 * reworked before the SDK is supported.
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, expect, it } from "@jest/globals";

/** [export name, prototype method] pairs the legacy hooks wrap. */
const LEGACY_TARGETS: Array<[string, string]> = [
  ["Agent", "run"],
  ["Agent", "invoke"],
  ["Agent", "stream"],
  ["Tool", "execute"],
  ["Runner", "run"],
  ["Session", "sendMessage"],
];

function legacyTargetsIn(sdk: any): string[] {
  return LEGACY_TARGETS
    .filter(([cls, method]) => typeof sdk?.[cls]?.prototype?.[method] === "function")
    .map(([cls, method]) => `${cls}.${method}`);
}

const probeSource = `
  import { readFileSync } from "node:fs";
  import { dirname, join } from "node:path";
  import { fileURLToPath } from "node:url";
  const sdk = await import("@google/adk");
  const targets = ${JSON.stringify(LEGACY_TARGETS)};
  const found = targets.filter(([c, m]) => typeof sdk?.[c]?.prototype?.[m] === "function").map(([c, m]) => c + "." + m);
  // The package's own package.json is not an export: walk up from the resolved entry point.
  let dir = dirname(fileURLToPath(import.meta.resolve("@google/adk")));
  let version = "unknown";
  for (; dir !== dirname(dir); dir = dirname(dir)) {
    try {
      const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
      if (pkg.name === "@google/adk") { version = pkg.version; break; }
    } catch { /* keep walking */ }
  }
  console.log(JSON.stringify({ version, found }));
`;

/** Directories whose node_modules may hold an @google/adk install: this package, plus TRACEAI_ADK_PROBE_DIRS. */
function candidateDirs(): string[] {
  const extra = (process.env.TRACEAI_ADK_PROBE_DIRS ?? "").split(":").filter(Boolean);
  return [join(__dirname, "..", ".."), ...extra];
}

function installedIn(dir: string): boolean {
  try {
    require.resolve("@google/adk/package.json", { paths: [dir] });
    return true;
  } catch {
    try {
      readFileSync(join(dir, "node_modules", "@google", "adk", "package.json"));
      return true;
    } catch {
      return false;
    }
  }
}

describe("legacy whole-object hooks", () => {
  it("the targets listed here are exactly the ones the hooks wrap", () => {
    const source = readFileSync(join(__dirname, "..", "instrumentation.ts"), "utf8");
    const wrapped = [...source.matchAll(/adkModule\??\.(\w+)\?\.prototype\?\.(\w+)/g)]
      .map(([, cls, method]) => `${cls}.${method}`);
    expect([...new Set(wrapped)].sort()).toEqual(LEGACY_TARGETS.map(([c, m]) => `${c}.${m}`).sort());
  });

  it("the automatic loader attaches only to the version ranges verified to expose no hook target", () => {
    // Verified with the probe below: @google/adk 0.1.0, 0.2.5, 1.0.0 and 1.6.0 expose none of the targets. From 2.x,
    // Agent.prototype.run exists (inherited), so the loader range must not include 2.x until the hooks are reworked.
    const source = readFileSync(join(__dirname, "..", "instrumentation.ts"), "utf8");
    const ranges = source.match(/"@google\/adk",\s*(\[[^\]]*\])/);
    expect(ranges).not.toBeNull();
    expect(JSON.parse((ranges as RegExpMatchArray)[1].replace(/'/g, '"'))).toEqual(["^0.1.0", "^0.2.0", "^1.0.0"]);
  });

  it("the check finds a hook target on an SDK that exposes one", () => {
    class Agent { run() { return undefined; } }
    class Runner { runAsync() { return undefined; } }
    expect(legacyTargetsIn({ Agent, Runner })).toEqual(["Agent.run"]);
    expect(legacyTargetsIn({ Agent: class {}, Runner })).toEqual([]);
  });

  it("no installed @google/adk version exposes a hook target", () => {
    const probed: string[] = [];
    for (const dir of candidateDirs()) {
      if (!installedIn(dir)) continue;
      const out = execFileSync(process.execPath, ["--input-type=module", "-e", probeSource], { cwd: dir, encoding: "utf8" });
      const { version, found } = JSON.parse(out.trim().split("\n").pop() as string) as { version: string; found: string[] };
      probed.push(version);
      expect({ version, found }).toEqual({ version, found: [] });
    }
    // Without an installed SDK the check above has nothing to read; say so rather than pass silently.
    if (probed.length === 0) {
      // eslint-disable-next-line no-console
      console.warn("@google/adk is not installed here; set TRACEAI_ADK_PROBE_DIRS to directories that have it to probe real versions.");
    }
  });
});
